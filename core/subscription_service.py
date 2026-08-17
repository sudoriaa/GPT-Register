# -*- coding: utf-8 -*-
"""ChatGPT subscription cancellation queue and browser workflow.

The public entry point is :func:`enqueue_account_subscription_cancel`.  The
worker deliberately starts with the existing access token: accounts that are
already cancelled, do not have a subscription, or were purchased through a
mobile store never open a browser.
"""
from __future__ import annotations

import importlib
import json
import logging
import random
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from core import chatgpt_plan, db
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)

# Browser helpers are imported only when a task reaches the UI path.  This
# keeps queue/status-only deployments usable even when optional browser/TOTP
# packages are not installed in the lightweight process.
roxy_registration = None
roxy_codex_oauth = None
account_liveness = None

_WORKERS = 2
_MAX_BATCH_CONCURRENCY = 20
_QUEUE_LIMIT = 200
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="subscription-cancel")
_BATCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=_MAX_BATCH_CONCURRENCY,
    thread_name_prefix="subscription-cancel-batch",
)
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_LOG_DIR = Path(__file__).resolve().parent.parent / "销套餐日志"
_CANCEL_LOG_LOCAL = threading.local()
_CANCELLING: set[str] = set()
_CANCELLING_LOCK = threading.Lock()
def _proxy_retry_limit() -> int:
    try:
        from config import proxy as proxy_cfg
        value = int(getattr(proxy_cfg, "PROXY_RETRY_MAX_ATTEMPTS", 4) or 4)
    except (ImportError, TypeError, ValueError):
        value = 4
    return max(1, min(4, value))


_PROTOCOL_MAX_PROXY_ATTEMPTS = _proxy_retry_limit()
_LOGIN_REFRESH_MAX_ATTEMPTS = _proxy_retry_limit()
_LOGIN_REFRESH_RETRY_DELAY = 1.0

_LOGIN_URL = "https://chatgpt.com/auth/login"
_BILLING_URL = "https://chatgpt.com/#settings/Billing"
_MOBILE_PURCHASE_ORIGINS = {
    "apple",
    "app_store",
    "appstore",
    "ios",
    "google",
    "google_play",
    "googleplay",
    "android",
}

# These are exact accessible-name candidates.  Generic "Continue"/"OK" and
# first-button fallbacks are intentionally absent.
_MANAGE_SUBSCRIPTION_TEXTS = (
    "Manage subscription",
    "Manage my subscription",
    "Manage plan",
    "管理订阅",
    "管理我的订阅",
    "管理方案",
    "管理訂閱",
    "サブスクリプションを管理",
    "プランを管理",
)
_CANCEL_ENTRY_TEXTS = (
    "Cancel plan",
    "Cancel subscription",
    "Cancel my subscription",
    "Cancel your subscription",
    "End subscription",
    "取消套餐",
    "取消订阅",
    "取消我的订阅",
    "终止订阅",
    "取消方案",
    "取消訂閱",
    "終止訂閱",
    "プランをキャンセル",
    "サブスクリプションをキャンセル",
    "定期購入を解約",
    "プランを解約",
    "解約する",
)
_CANCEL_SECTION_HEADINGS = (
    "Cancel plan",
    "Cancel subscription",
    "取消套餐",
    "取消订阅",
    "取消方案",
    "取消訂閱",
    "プランをキャンセル",
    "サブスクリプションをキャンセル",
    "定期購入の解約",
)
_CANCEL_SECTION_BUTTON_TEXTS = (
    "Cancel",
    "取消",
    "解約",
    "キャンセル",
)
_CANCEL_CONFIRM_TEXTS = (
    "Confirm cancellation",
    "Confirm cancel",
    "Yes, cancel",
    "Yes, cancel subscription",
    "Cancel subscription",
    "确认取消",
    "确认取消订阅",
    "继续取消",
    "是的，取消订阅",
    "確認取消",
    "確認取消訂閱",
    "解約を確定",
    "解約を確認",
    "はい、解約します",
    "サブスクリプションをキャンセル",
)

_EXACT_ACTION_JS = r"""
const wanted = new Set((arguments[0] || []).map(v =>
  String(v || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase()
));
const visible = el => {
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.visibility !== 'hidden' && style.display !== 'none' &&
         rect.width > 0 && rect.height > 0 && !el.disabled &&
         el.getAttribute('aria-disabled') !== 'true';
};
const names = el => [
  el.getAttribute('aria-label'),
  el.getAttribute('title'),
  el.value,
  el.innerText,
  el.textContent,
].map(v => String(v || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
const nodes = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')];
for (const el of nodes) {
  if (!visible(el)) continue;
  const matched = names(el).find(v => wanted.has(v.toLocaleLowerCase()));
  if (!matched) continue;
  el.scrollIntoView({block: 'center', inline: 'center'});
  el.click();
  return {ok: true, name: matched, tag: String(el.tagName || '').toLowerCase()};
}
return {ok: false};
"""

_SCOPED_CANCEL_ENTRY_JS = r"""
const wantedHeadings = new Set((arguments[0] || []).map(v =>
  String(v || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase()
));
const wantedButtons = new Set((arguments[1] || []).map(v =>
  String(v || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase()
));
const normalize = value => String(value || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase();
const visible = el => {
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.visibility !== 'hidden' && style.display !== 'none' &&
         rect.width > 0 && rect.height > 0 && !el.disabled &&
         el.getAttribute('aria-disabled') !== 'true';
};
const nameOf = el => [
  el.getAttribute('aria-label'), el.getAttribute('title'), el.value,
  el.innerText, el.textContent,
].map(normalize).find(value => wantedButtons.has(value)) || '';
const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')]
  .filter(el => visible(el) && wantedHeadings.has(normalize(el.innerText || el.textContent)));
for (const heading of headings) {
  let scope = heading.parentElement;
  for (let depth = 0; scope && depth < 6; depth += 1, scope = scope.parentElement) {
    const matches = [...scope.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(visible)
      .map(el => ({el, name: nameOf(el)}))
      .filter(item => item.name);
    const unique = matches.filter((item, index) => matches.findIndex(other => other.el === item.el) === index);
    if (unique.length === 1) {
      unique[0].el.scrollIntoView({block: 'center', inline: 'center'});
      unique[0].el.click();
      return {ok: true, name: unique[0].name, scoped: true};
    }
  }
}
return {ok: false};
"""

_INSTALL_REQUEST_CAPTURE_JS = r"""
(() => {
  if (window.__subscriptionCancelCaptureInstalled) return true;
  window.__subscriptionCancelCaptureInstalled = true;
  window.__subscriptionCancelRequests = [];
  const pathOnly = value => {
    try { return new URL(String(value || ''), location.href).pathname || '/'; }
    catch (_) { return ''; }
  };
  const record = (url, method, status) => {
    const path = pathOnly(url);
    if (!path) return;
    window.__subscriptionCancelRequests.push({
      path,
      method: String(method || 'GET').toUpperCase(),
      status: Number.isFinite(Number(status)) ? Number(status) : null,
    });
    if (window.__subscriptionCancelRequests.length > 40) {
      window.__subscriptionCancelRequests.splice(0, window.__subscriptionCancelRequests.length - 40);
    }
  };
  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    window.fetch = function(input, init) {
      const url = input && input.url ? input.url : input;
      const method = (init && init.method) || (input && input.method) || 'GET';
      return originalFetch.apply(this, arguments).then(response => {
        record(url, method, response && response.status);
        return response;
      });
    };
  }
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__subscriptionCancelMeta = {method, url};
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    this.addEventListener('loadend', () => {
      const meta = this.__subscriptionCancelMeta || {};
      record(meta.url, meta.method, this.status);
    }, {once: true});
    return originalSend.apply(this, arguments);
  };
  return true;
})();
"""

_READ_REQUEST_CAPTURE_JS = (
    "return Array.isArray(window.__subscriptionCancelRequests) "
    "? window.__subscriptionCancelRequests.slice(-40) : [];"
)

_BROWSER_PLAN_CHECK_JS = r"""
const token = String(arguments[0] || '');
const path = String(arguments[1] || '/backend-api/accounts/check');
const done = arguments[arguments.length - 1];
const separator = path.includes('?') ? '&' : '?';
fetch(path + separator + 'timezone_offset_min=-', {
  method: 'GET',
  credentials: 'include',
  headers: {accept: 'application/json', authorization: 'Bearer ' + token},
}).then(async response => {
  let data = null;
  try { data = await response.json(); } catch (_) {}
  done({ok: response.ok, status: response.status, data});
}).catch(error => done({ok: false, status: null, error: String(error && error.message || error)}));
"""


def _load_roxy_helpers():
    global roxy_registration, roxy_codex_oauth
    if roxy_registration is None:
        roxy_registration = importlib.import_module("core.roxy_registration")
    if roxy_codex_oauth is None:
        roxy_codex_oauth = importlib.import_module("core.roxy_codex_oauth")
    return roxy_registration, roxy_codex_oauth


def _load_account_liveness():
    global account_liveness
    if account_liveness is None:
        account_liveness = importlib.import_module("core.account_liveness")
    return account_liveness


def log_path(email: str) -> Path:
    """Return the per-account cancellation log path without exposing secrets."""
    value = str(email or "").strip()
    safe = re.sub(r"[^A-Za-z0-9@._+\-]", "_", value).strip("._") or "unknown"
    return _LOG_DIR / f"subscription-cancel-{safe[:180]}.log"


def read_cancel_log(email: str, max_bytes: int = 80_000) -> str:
    """Read the tail of one cancellation log for the WebUI log endpoint."""
    path = log_path(email)
    if not path.exists() or not path.is_file():
        return ""
    try:
        limit = max(1_000, min(1_000_000, int(max_bytes or 80_000)))
    except (TypeError, ValueError):
        limit = 80_000
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            content = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    # Keep generic credentials out even if a future controlled log message
    # accidentally includes an Authorization header or credentialed proxy.
    return _redact(content, limit=None)


def is_cancelling(email: str) -> bool:
    key = str(email or "").strip().casefold()
    if not key:
        return False
    with _CANCELLING_LOCK:
        return key in _CANCELLING


def _set_cancelling(email: str, active: bool) -> None:
    key = str(email or "").strip().casefold()
    if not key:
        return
    with _CANCELLING_LOCK:
        if active:
            _CANCELLING.add(key)
        else:
            _CANCELLING.discard(key)


@contextmanager
def _cancel_log_session(email: str, secrets: Iterable[object] = ()):
    """Create one controlled per-account log writer for the current worker."""
    path = log_path(email)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    except OSError:
        pass
    previous = getattr(_CANCEL_LOG_LOCAL, "state", None)
    _CANCEL_LOG_LOCAL.state = {
        "path": path,
        "secrets": [email, *list(secrets)],
    }
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_CANCEL_LOG_LOCAL, "state")
            except AttributeError:
                pass
        else:
            _CANCEL_LOG_LOCAL.state = previous


def _cancel_log_add_secret(value: object) -> None:
    state = getattr(_CANCEL_LOG_LOCAL, "state", None)
    if isinstance(state, dict) and value:
        state.setdefault("secrets", []).append(value)


def _cancel_log(message: object, *, level: str = "INFO") -> None:
    """Append a controlled, redacted line; helper response bodies never enter it."""
    state = getattr(_CANCEL_LOG_LOCAL, "state", None)
    if not isinstance(state, dict):
        return
    safe = _redact(message, state.get("secrets") or ())
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} [{str(level or 'INFO').upper()[:10]}] {safe}\n"
    try:
        with Path(state["path"]).open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _mask_email(email: str) -> str:
    value = str(email or "").strip()
    if "@" not in value:
        return "***"
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _stored_login_credentials(account: dict) -> tuple[str, str]:
    password = str(account.get("chatgpt_password") or account.get("password") or "").strip()
    if not password:
        try:
            extra = json.loads(str(account.get("extra_json") or "{}"))
            if isinstance(extra, dict):
                password = str(extra.get("registration_password") or "").strip()
        except Exception:
            password = ""
    return password, str(account.get("totp_secret") or "").strip()


def _redact(
    value: object,
    secrets: Iterable[object] = (),
    *,
    limit: int | None = 800,
) -> str:
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
    text = re.sub(
        r"(?i)(?<![a-z0-9._-])[^/\s:@]+:[^@\s/]+@",
        "[REDACTED]@",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~+\-/=]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\beyJ[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]{8,}(?:\.[a-zA-Z0-9_-]+)?\b", "[REDACTED]", text)
    return text if limit is None else text[:max(0, int(limit))]


def _redact_payload(value: object, secrets: Iterable[object]) -> object:
    if isinstance(value, dict):
        return {str(key): _redact_payload(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_payload(item, secrets) for item in value)
    if isinstance(value, str):
        return _redact(value, secrets)
    return value


class _SensitiveLogFilter(logging.Filter):
    """Redact this worker's account material from helper-module log records."""

    def __init__(self, secrets: Iterable[object]):
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
def _redacted_helper_logs(secrets: Iterable[object]):
    scrubber = _SensitiveLogFilter(secrets)
    target_loggers = [
        logging.getLogger(__name__),
        logging.getLogger("core.account_liveness"),
        logging.getLogger("core.chatgpt_plan"),
        logging.getLogger("core.roxybrowser_client"),
        logging.getLogger("core.roxy_registration"),
        logging.getLogger("core.roxy_codex_oauth"),
    ]
    for target in target_loggers:
        target.addFilter(scrubber)
    try:
        yield scrubber
    finally:
        for target in target_loggers:
            target.removeFilter(scrubber)


def _needs_access_token_refresh(result: object) -> bool:
    """Recognize the explicit auth failures returned by plan/cancel protocols."""
    if not isinstance(result, dict):
        return False
    if result.get("needs_live_check") is True or result.get("token_expired") is True:
        return True
    if str(result.get("reason") or "").strip().lower() in {
        "token_expired",
        "access_token_expired",
        "missing_token",
        "unauthorized",
    }:
        return True
    for key in ("http_status", "cancel_http_status", "verify_http_status"):
        try:
            if int(result.get(key)) == 401:
                return True
        except (TypeError, ValueError):
            pass
    error = str(result.get("error") or "").strip().lower()
    return any(marker in error for marker in (
        "http 401",
        "status 401",
        "access token is expired",
        "at已过期",
        "at 已过期",
        "unauthorized",
    ))


def _login_refresh_retryable(exc: BaseException) -> bool:
    """Return False only for errors that another browser session cannot fix."""
    if isinstance(exc, ValueError) or type(exc).__name__ == "AccountUnusableError":
        return False
    detail = f"{type(exc).__name__}: {exc}".lower()
    terminal_markers = (
        "wrong password",
        "incorrect password",
        "invalid password",
        "密码错误",
        "密码不正确",
        "account_email_mismatch",
        "账号与待销套餐账号不一致",
        "account deactivated",
        "account disabled",
        "账号已停用",
        "账号已废",
        "账号已删除",
    )
    return not any(marker in detail for marker in terminal_markers)


def _refresh_access_token_via_password_totp(
    *,
    account_id: int,
    email: str,
    password: str,
    totp_secret: str,
    redactor: _SensitiveLogFilter,
) -> str:
    """Refresh ChatGPT AT in Roxy with the stored password and TOTP only."""
    missing = []
    if not str(password or "").strip():
        missing.append("chatgpt_password/password")
    if not str(totp_secret or "").strip():
        missing.append("totp_secret")
    if missing:
        raise ValueError(
            f"刷新 AT 缺少本地登录凭据: {', '.join(missing)}；未读取邮箱验证码"
        )
    _cancel_log("检测到 AT 失效，开始 Roxy 浏览器密码+2FA 登录（不读取邮箱验证码）")
    liveness = _load_account_liveness()
    last_exc: BaseException | None = None
    for attempt in range(1, _LOGIN_REFRESH_MAX_ATTEMPTS + 1):
        try:
            _cancel_log(f"刷新 AT 登录节点第 {attempt}/{_LOGIN_REFRESH_MAX_ATTEMPTS} 次")
            login_result = liveness.relogin_account_with_password_totp(
                email,
                password,
                totp_secret,
            )
            if not isinstance(login_result, dict):
                raise RuntimeError("Roxy 密码+2FA 登录返回格式异常")
            session_info = login_result.get("session")
            if not isinstance(session_info, dict):
                raise RuntimeError("Roxy 密码+2FA 登录未返回有效 session")
            access_token = str(login_result.get("access_token") or "").strip()
            session_access_token = str(session_info.get("accessToken") or "").strip()
            if access_token:
                redactor.add_secret(access_token)
                _cancel_log_add_secret(access_token)
            if not access_token or not session_access_token:
                raise RuntimeError("Roxy 密码+2FA 登录未返回 accessToken")
            if access_token != session_access_token:
                raise RuntimeError("Roxy 密码+2FA 登录返回的 accessToken 不一致")

            claims = chatgpt_plan.token_claims(access_token)
            claim_email = str(claims.get("email") or "").strip()
            if claim_email and claim_email.casefold() != email.casefold():
                raise RuntimeError("Roxy 登录账号与待销套餐账号不一致")
            if claims.get("token_expired") is True:
                raise RuntimeError("Roxy 密码+2FA 登录返回的 accessToken 已失效")

            refreshed = {
                "ok": True,
                "status": "live",
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "access_token": access_token,
                "session": session_info,
                "network_route": str(login_result.get("network_route") or "roxy_browser"),
            }
            if not db.update_account_liveness(account_id, refreshed):
                raise RuntimeError("账号已删除，最新 AT 写回失败")
            _cancel_log("Roxy 密码+2FA 登录成功，最新 AT 已写回账号；继续协议销套餐")
            return access_token
        except Exception as exc:
            last_exc = exc
            if not _login_refresh_retryable(exc) or attempt >= _LOGIN_REFRESH_MAX_ATTEMPTS:
                raise
            _cancel_log(
                f"刷新 AT 登录节点失败，第 {attempt}/{_LOGIN_REFRESH_MAX_ATTEMPTS} 次；"
                "回退到新建 Roxy 环境后重试 "
                f"type={type(exc).__name__}",
                level="WARNING",
            )
            time.sleep(_LOGIN_REFRESH_RETRY_DELAY * attempt)

    raise RuntimeError("Roxy 密码+2FA 登录重试未执行") from last_exc


def _optional_bool(value: object) -> bool | None:
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


def _purchase_origin(result: dict) -> str:
    raw = result.get("last_purchase_origin_platform") or result.get("purchase_origin_platform") or ""
    return re.sub(r"[\s-]+", "_", str(raw).strip().lower())


def _is_mobile_purchase_origin(origin: str) -> bool:
    value = str(origin or "").strip().lower()
    if value in _MOBILE_PURCHASE_ORIGINS:
        return True
    return any(marker in value for marker in ("app_store", "appstore", "google_play", "ios", "android"))


def _subscription_disposition(result: dict) -> str:
    """Return mobile_store, none, already_cancelled, cancel, or unknown."""
    if _is_mobile_purchase_origin(_purchase_origin(result)):
        return "mobile_store"

    status = str(result.get("subscription_status") or "").strip().lower()
    active = _optional_bool(result.get("has_active_subscription"))
    will_renew = _optional_bool(
        result.get("last_will_renew")
        if "last_will_renew" in result
        else result.get("will_renew")
    )
    cancels_at = (
        result.get("cancels_at")
        or result.get("plan_cancels_at")
        or result.get("subscription_cancels_at")
    )

    if cancels_at or status in {"cancel_scheduled", "active_nonrenewing"}:
        return "already_cancelled"
    if active is True and will_renew is False:
        return "already_cancelled"
    if active is False or status == "none":
        return "none"

    plan = str(result.get("current_plan_type") or result.get("plan_type") or "").strip().lower()
    subscription_plan = str(result.get("subscription_plan") or "").strip().lower()
    if active is not True and plan == "free" and subscription_plan in {"", "chatgptfreeplan"}:
        return "none"
    if active is True or will_renew is True or status == "renewing":
        return "cancel"
    return "unknown"


def _is_cancel_confirmed(result: dict) -> bool:
    if not isinstance(result, dict) or not result.get("ok"):
        return False
    if _optional_bool(result.get("has_active_subscription")) is False:
        return True
    cancels_at = (
        result.get("cancels_at")
        or result.get("plan_cancels_at")
        or result.get("subscription_cancels_at")
    )
    if cancels_at:
        return True
    value = result.get("last_will_renew") if "last_will_renew" in result else result.get("will_renew")
    return _optional_bool(value) is False


def _result(*, status: str, reason: str, protocol: str, error: str | None = None,
            request: list[dict] | None = None) -> dict:
    payload = {
        "ok": status in {"success", "skipped"},
        "status": status,
        "reason": reason,
        "protocol": protocol,
        "done_at": datetime.now().isoformat(timespec="seconds"),
    }
    if error:
        payload["error"] = error
    if request is not None:
        payload["request"] = request
    return payload


def _with_attempt_meta(result: dict, source: object) -> dict:
    if not isinstance(source, dict):
        return result
    for key in ("attempt_count", "max_attempts"):
        try:
            value = int(source.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            result[key] = value
    return result


def _protocol_failure_detail(
    phase: str,
    result: object,
    *,
    secrets: Iterable[object] = (),
) -> str:
    """Return a bounded, redacted diagnostic suitable for logs and task state."""
    payload = result if isinstance(result, dict) else {}

    def value(key: str, default: str = "-") -> str:
        raw = payload.get(key)
        if raw is None or raw == "":
            return default
        if isinstance(raw, (dict, list, tuple)):
            try:
                raw = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                raw = str(raw)
        return str(raw).replace("\r", " ").replace("\n", " ").strip() or default

    preview = value("response_preview")
    if preview == "-":
        preview = value("response_body_preview")
    preview = preview[:600]
    error = value("error")[:800]
    try:
        attempts = f"{int(payload.get('attempt_count'))}/{int(payload.get('max_attempts'))}"
    except (TypeError, ValueError):
        attempts = "-"
    detail = (
        f"phase={str(phase or 'unknown')[:32]} "
        f"reason={value('reason')[:120]} "
        f"http_status={value('http_status')[:32]} "
        f"cancel_http_status={value('cancel_http_status')[:32]} "
        f"verify_http_status={value('verify_http_status')[:32]} "
        f"attempts={attempts} "
        f"error={error} "
        f"response_preview={preview}"
    )
    return _redact(detail, secrets, limit=1_800)


def _protocol_proxy_candidates(limit: int = _PROTOCOL_MAX_PROXY_ATTEMPTS) -> list[str]:
    """Return protocol routes with the OS proxy reserved as the final fallback."""
    from config import proxy as proxy_cfg

    limit = max(1, min(_PROTOCOL_MAX_PROXY_ATTEMPTS, int(limit or 1)))
    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效")
    if mode == "direct":
        return [""]

    dedicated = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    configured_pool = getattr(proxy_cfg, "PROXY_POOL", ()) or ()
    if isinstance(configured_pool, str):
        configured_pool = [configured_pool]
    pool = [str(item or "").strip() for item in configured_pool if str(item or "").strip()]
    random.shuffle(pool)
    system_proxy = chatgpt_plan._system_proxy_url()

    candidates: list[str] = []
    seen: set[str] = set()
    normal_limit = limit - (1 if system_proxy else 0)
    for candidate in ([dedicated] if dedicated else []) + pool:
        if len(candidates) >= normal_limit:
            break
        if candidate in seen:
            continue
        if system_proxy and chatgpt_plan._same_proxy_endpoint(candidate, system_proxy):
            continue
        seen.add(candidate)
        candidates.append(candidate)
    if system_proxy and len(candidates) < limit:
        candidates.append(system_proxy)
    if system_proxy and candidates == [system_proxy] and limit > 1:
        # With no alternate upstream, retry the system path for transient
        # transport errors while preserving the same global attempt budget.
        candidates.extend(system_proxy for _ in range(limit - 1))
    if not candidates and mode == "auto":
        # Preserve auto mode's existing direct behavior when no proxy is configured.
        return [""]
    return candidates


def _protocol_proxy_label(proxy: str) -> str:
    if not str(proxy or "").strip():
        return "direct"
    safe = _redact(proxy, limit=160)
    # Non-URL proxy formats are not expected, but never echo a possible
    # username/password pair if one is supplied by configuration.
    if "@" in str(proxy) and "[REDACTED]@" not in safe:
        return "configured-proxy"
    return safe


def _query_plan_with_proxy_rotation(
    access_token: str,
    *,
    proxies: Iterable[str] | None = None,
    attempt_offset: int = 0,
    max_attempts: int = _PROTOCOL_MAX_PROXY_ATTEMPTS,
    secrets: Iterable[object] = (),
) -> tuple[dict, int]:
    """Query the account plan once per distinct proxy, up to the hard limit."""
    max_attempts = max(1, min(_PROTOCOL_MAX_PROXY_ATTEMPTS, int(max_attempts or 1)))
    used = max(0, int(attempt_offset or 0))
    candidates = list(proxies) if proxies is not None else _protocol_proxy_candidates(max_attempts)
    candidates = candidates[:max(0, max_attempts - used)]
    last: dict = {
        "ok": False,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "error": "未配置可用的协议查询代理",
    }

    for proxy in candidates:
        used += 1
        label = _protocol_proxy_label(proxy)
        _cancel_log(f"协议查询第 {used}/{max_attempts} 次，代理={label}")
        try:
            raw = chatgpt_plan.check_account_plan(
                access_token,
                proxy=proxy,
                max_attempts=1,
                retry_delay=0,
                include_subscription=True,
            )
            if isinstance(raw, dict):
                redacted = _redact_payload(raw, secrets)
                last = redacted if isinstance(redacted, dict) else {}
            else:
                last = {
                    "ok": False,
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "error": "协议套餐查询返回格式异常",
                }
        except Exception as exc:
            last = {
                "ok": False,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "error": _redact(f"{type(exc).__name__}: {exc}", secrets),
            }
        last["attempt_count"] = used
        last["max_attempts"] = max_attempts
        if last.get("ok"):
            _cancel_log(f"协议查询成功，第 {used}/{max_attempts} 次")
            return last, used
        if _needs_access_token_refresh(last):
            _cancel_log(
                f"协议查询检测到 AT 失效，第 {used}/{max_attempts} 次；"
                f"{_protocol_failure_detail('query', last, secrets=secrets)}",
                level="WARNING",
            )
            return last, used
        next_action = (
            "切换下一个代理"
            if used < max_attempts and len(candidates) > used - attempt_offset
            else "本轮代理已用完"
        )
        _cancel_log(
            f"协议查询失败，第 {used}/{max_attempts} 次；{next_action}；"
            f"{_protocol_failure_detail('query', last, secrets=secrets)}",
            level="WARNING",
        )

    last["attempt_count"] = used
    last["max_attempts"] = max_attempts
    _cancel_log(
        f"协议查询连续 {used}/{max_attempts} 次失败，任务停止；未进入浏览器兜底",
        level="ERROR",
    )
    return last, used


def _terminal_protocol_cancel(result: dict) -> bool:
    reason = str(result.get("reason") or "")
    if reason not in {"cancel_confirmed", "already_cancelled", "none", "mobile_store"}:
        return False
    if not result.get("ok"):
        return False
    return reason != "cancel_confirmed" or result.get("confirmed") is True


def _cancel_attempt_requires_read_only_verification(result: dict) -> bool:
    """A possibly accepted POST must never be submitted a second time."""
    if result.get("posted") is not True:
        return False
    status = result.get("cancel_http_status")
    if status is None:
        return True
    try:
        value = int(status)
    except (TypeError, ValueError):
        return True
    return 200 <= value < 300


def _verification_result_from_plan(plan: dict) -> dict:
    if str(plan.get("reason") or "") == "account_email_mismatch":
        return {
            **plan,
            "ok": False,
            "confirmed": False,
            "posted": True,
            "reason": "account_email_mismatch",
        }
    if plan.get("ok"):
        disposition = _subscription_disposition(plan)
        if disposition in {"already_cancelled", "none"} or _is_cancel_confirmed(plan):
            return {
                "ok": True,
                "confirmed": True,
                "posted": True,
                "reason": "cancel_confirmed",
                "after": plan,
            }
        if disposition == "mobile_store":
            return {
                "ok": True,
                "confirmed": False,
                "posted": True,
                "reason": "mobile_store",
                "after": plan,
            }
        return {
            "ok": False,
            "confirmed": False,
            "posted": True,
            "reason": "protocol_cancel_unconfirmed",
            "after": plan,
            "error": "取消请求已发送，但协议复查仍显示订阅会续费",
        }
    return {
        **plan,
        "ok": False,
        "confirmed": False,
        "posted": True,
        "reason": "protocol_cancel_verification_failed",
        "error": str(plan.get("error") or "取消请求已发送，但协议复查失败"),
    }


def _cancel_with_proxy_rotation(
    access_token: str,
    *,
    expected_email: str,
    proxies: Iterable[str] | None = None,
    attempt_offset: int = 0,
    verification_only: bool = False,
    max_attempts: int = _PROTOCOL_MAX_PROXY_ATTEMPTS,
    secrets: Iterable[object] = (),
) -> tuple[dict, int, bool]:
    """Cancel once per proxy; after an ambiguous POST, only query state."""
    max_attempts = max(1, min(_PROTOCOL_MAX_PROXY_ATTEMPTS, int(max_attempts or 1)))
    used = max(0, int(attempt_offset or 0))
    candidates = list(proxies) if proxies is not None else _protocol_proxy_candidates(max_attempts)
    candidates = candidates[:max(0, max_attempts - used)]
    read_only = bool(verification_only)
    last: dict = {
        "ok": False,
        "confirmed": False,
        "posted": read_only,
        "reason": "protocol_cancel_failed",
        "error": "未配置可用的协议取消代理",
    }

    for proxy in candidates:
        used += 1
        action = "取消结果复查" if read_only else "协议取消"
        label = _protocol_proxy_label(proxy)
        _cancel_log(f"{action}第 {used}/{max_attempts} 次，代理={label}")
        try:
            if read_only:
                raw_plan = chatgpt_plan.verify_account_subscription_protocol(
                    access_token,
                    expected_email=expected_email,
                    proxy=proxy,
                )
                if not isinstance(raw_plan, dict):
                    raw_plan = {"ok": False, "error": "协议复查返回格式异常"}
                redacted_plan = _redact_payload(raw_plan, secrets)
                plan = redacted_plan if isinstance(redacted_plan, dict) else {}
                last = _verification_result_from_plan(plan)
            else:
                raw_cancel = chatgpt_plan.cancel_account_subscription_protocol(
                    access_token,
                    expected_email=expected_email,
                    proxy=proxy,
                )
                if isinstance(raw_cancel, dict):
                    redacted_cancel = _redact_payload(raw_cancel, secrets)
                    last = redacted_cancel if isinstance(redacted_cancel, dict) else {}
                else:
                    last = {
                        "ok": False,
                        "confirmed": False,
                        "posted": False,
                        "reason": "protocol_cancel_invalid",
                        "error": "协议取消返回格式异常",
                    }
        except Exception as exc:
            last = {
                "ok": False,
                "confirmed": False,
                "posted": read_only,
                "reason": "protocol_cancel_exception",
                "error": _redact(f"{type(exc).__name__}: {exc}", secrets),
            }

        last["attempt_count"] = used
        last["max_attempts"] = max_attempts
        if _terminal_protocol_cancel(last):
            _cancel_log(
                f"协议取消流程完成，第 {used}/{max_attempts} 次，reason={str(last.get('reason') or '')[:64]}"
            )
            return last, used, read_only
        if str(last.get("reason") or "") == "account_email_mismatch":
            _cancel_log(
                _protocol_failure_detail("cancel", last, secrets=secrets),
                level="ERROR",
            )
            return last, used, read_only
        if _needs_access_token_refresh(last):
            _cancel_log(
                f"协议取消检测到 AT 失效，第 {used}/{max_attempts} 次；"
                f"{_protocol_failure_detail(action, last, secrets=secrets)}",
                level="WARNING",
            )
            return last, used, read_only
        if not read_only and _cancel_attempt_requires_read_only_verification(last):
            read_only = True
            _cancel_log("取消请求可能已被服务端接收，后续代理仅复查状态，不重复提交取消请求")
        next_action = (
            "切换下一个代理"
            if used < max_attempts and len(candidates) > used - attempt_offset
            else "本轮代理已用完"
        )
        _cancel_log(
            f"{action}失败，第 {used}/{max_attempts} 次；{next_action}；"
            f"{_protocol_failure_detail(action, last, secrets=secrets)}",
            level="WARNING",
        )

    last["attempt_count"] = used
    last["max_attempts"] = max_attempts
    _cancel_log(
        f"协议取消连续 {used}/{max_attempts} 次失败，任务停止；未进入浏览器兜底",
        level="ERROR",
    )
    return last, used, read_only


def _persist_cancel_result(account_id: int, email: str, result: dict) -> bool:
    reason = str(result.get("reason") or "")
    message = {
        "cancel_confirmed": "已关闭下个周期自动续费",
        "already_cancelled": "账号已处于停止续费状态",
        "none": "未检测到需要取消的有效订阅",
        "mobile_store": "移动应用商店订阅需在对应商店内管理",
        "missing_credentials": "缺少密码或 2FA 密钥，未执行登录",
    }.get(reason, str(result.get("error") or reason or "取消任务已完成"))
    _cancel_log(
        "任务结束 "
        f"status={str(result.get('status') or 'failed')[:32]} "
        f"reason={reason[:64] or '-'} "
        f"protocol={str(result.get('protocol') or 'unknown')[:32]}"
    )
    return db.update_account_subscription_cancel(
        acc_id=account_id,
        email=email,
        status=str(result.get("status") or "failed"),
        error=None if result.get("ok") else str(result.get("error") or "取消任务失败"),
        protocol=str(result.get("protocol") or "ui"),
        outcome=reason or None,
        message=message,
    )


def _click_exact_action(driver, candidates: Iterable[str], *, timeout: float, stage: str) -> dict:
    deadline = time.monotonic() + max(0.0, float(timeout))
    first = True
    while first or time.monotonic() < deadline:
        first = False
        found = driver.execute_script(_EXACT_ACTION_JS, list(candidates))
        if isinstance(found, dict) and found.get("ok"):
            return {
                "ok": True,
                "name": str(found.get("name") or "")[:100],
                "tag": str(found.get("tag") or "")[:20],
            }
        time.sleep(0.5)
    raise RuntimeError(f"{stage}未找到精确匹配的操作按钮")


def _click_scoped_cancel_entry(driver, *, timeout: float = 12) -> dict:
    """Click a short localized Cancel button only inside its billing section."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    first = True
    while first or time.monotonic() < deadline:
        first = False
        found = driver.execute_script(
            _SCOPED_CANCEL_ENTRY_JS,
            list(_CANCEL_SECTION_HEADINGS),
            list(_CANCEL_SECTION_BUTTON_TEXTS),
        )
        if isinstance(found, dict) and found.get("ok"):
            return {
                "ok": True,
                "name": str(found.get("name") or "")[:100],
                "scoped": True,
            }
        time.sleep(0.5)
    raise RuntimeError("取消套餐区块内未找到唯一的取消按钮")


def _switch_to_latest_window(driver) -> None:
    try:
        handles = list(driver.window_handles or [])
        current = str(driver.current_window_handle or "")
        candidates = [handle for handle in handles if str(handle) != current]
        if candidates:
            driver.switch_to.window(candidates[-1])
    except Exception:
        return


def _is_chatgpt_origin(url: object) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    return host == "chatgpt.com" or host.endswith(".chatgpt.com")


def _open_cancellation_entry(driver) -> dict:
    try:
        return _click_exact_action(
            driver,
            _CANCEL_ENTRY_TEXTS,
            timeout=12,
            stage="取消入口",
        )
    except RuntimeError:
        try:
            # The current zh-CN Billing page labels the section "取消套餐"
            # but the button itself only "取消".  Scope that short label to
            # the matching section so another dialog's cancel button is never
            # selected globally.
            return _click_scoped_cancel_entry(driver, timeout=8)
        except RuntimeError:
            # Some Billing variants expose an exact "Manage subscription"
            # gateway before the explicit cancellation entry (often in a new tab).
            _click_exact_action(
                driver,
                _MANAGE_SUBSCRIPTION_TEXTS,
                timeout=10,
                stage="订阅管理入口",
            )
            time.sleep(1.0)
            _switch_to_latest_window(driver)
            try:
                return _click_exact_action(
                    driver,
                    _CANCEL_ENTRY_TEXTS,
                    timeout=30,
                    stage="取消入口",
                )
            except RuntimeError:
                return _click_scoped_cancel_entry(driver, timeout=12)


def _sanitize_cancel_requests(raw_records: object) -> list[dict]:
    records = raw_records if isinstance(raw_records, list) else []
    output: list[dict] = []
    seen: set[tuple[str, str, int | None]] = set()
    hints = ("cancel", "subscription", "billing", "checkout", "payment")
    for item in records:
        if not isinstance(item, dict):
            continue
        try:
            path = urlsplit(str(item.get("path") or "")).path
        except Exception:
            path = ""
        method = str(item.get("method") or "").strip().upper()
        try:
            status = int(item.get("status")) if item.get("status") is not None else None
        except (TypeError, ValueError):
            status = None
        if not path.startswith("/") or not method or not any(hint in path.lower() for hint in hints):
            continue
        path = re.sub(r"(?<!\d)\d{12,19}(?!\d)", "[REDACTED]", path)
        key = (path[:500], method[:12], status)
        if key in seen:
            continue
        seen.add(key)
        # No headers, cookies, request body, response body, card, or billing
        # fields cross this boundary.
        output.append({"path": key[0], "method": key[1], "status": key[2]})
    return output[-10:]


def _browser_account_plan(driver, access_token: str) -> dict:
    try:
        driver.set_script_timeout(35)
    except Exception:
        pass
    raw = driver.execute_async_script(
        _BROWSER_PLAN_CHECK_JS,
        access_token,
        chatgpt_plan.ACCOUNTS_CHECK_PATH,
    )
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": "浏览器套餐查询返回格式异常",
            "network_route": "browser_same_origin",
        }
    status = raw.get("status")
    if not raw.get("ok") or not isinstance(raw.get("data"), dict):
        return {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "http_status": status,
            "error": f"浏览器套餐查询失败: HTTP {status or '-'}",
            "network_route": "browser_same_origin",
        }
    try:
        result = chatgpt_plan.parse_accounts_check(raw["data"], token=access_token)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "http_status": status,
            "error": f"浏览器套餐解析失败: {type(exc).__name__}",
            "network_route": "browser_same_origin",
        }
    result["http_status"] = status
    result["network_route"] = "browser_same_origin"
    return _redact_payload(result, (access_token,))


def _verify_browser_cancellation(driver, access_token: str, account_id: int, *, attempts: int = 8) -> dict:
    last: dict = {}
    for attempt in range(1, max(1, attempts) + 1):
        last = _browser_account_plan(driver, access_token)
        db.update_account_plan_check(acc_id=account_id, result=last)
        if _is_cancel_confirmed(last):
            return last
        if attempt < attempts:
            time.sleep(2.0 * attempt)
    return last


def _cancel_via_browser(*, account_id: int, email: str, password: str,
                        totp_secret: str, redactor: _SensitiveLogFilter | None = None) -> dict:
    client: RoxyBrowserClient | None = None
    opened = None
    driver = None
    new_access_token = ""
    try:
        registration, codex_oauth = _load_roxy_helpers()
        client = RoxyBrowserClient()
        opened = client.open_profile()
        driver = registration._build_driver(opened)

        registration._safe_get(
            driver,
            _LOGIN_URL,
            timeout=45,
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
            script_timeout=35,
        )
        registration._type_email_address(driver, email, timeout=20)
        registration._submit_email_step(driver, email)
        login_outcome = codex_oauth._login_with_password_and_2fa(
            driver,
            email,
            password,
            totp_secret,
            timeout=60,
        )
        if login_outcome != "done":
            raise RuntimeError(f"密码/TOTP 登录流程未完成: {login_outcome or 'unknown'}")

        session = registration._fetch_chatgpt_session(driver, timeout=90)
        new_access_token = str((session or {}).get("accessToken") or "").strip()
        if not new_access_token:
            raise RuntimeError("登录后未读取到新 access token")
        if redactor is not None:
            redactor.add_secret(new_access_token)
        db.update_account_liveness(account_id, {
            "ok": True,
            "status": "live",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "access_token": new_access_token,
            "session": session,
        })

        # Verify the newly logged-in account from the browser origin before
        # entering Billing, and persist the refreshed plan snapshot.
        browser_plan = _browser_account_plan(driver, new_access_token)
        db.update_account_plan_check(acc_id=account_id, result=browser_plan)
        if not browser_plan.get("ok"):
            raise RuntimeError(str(browser_plan.get("error") or "浏览器套餐查询失败"))
        disposition = _subscription_disposition(browser_plan)
        if disposition == "already_cancelled":
            return _result(
                status="success",
                reason=disposition,
                protocol="browser",
                request=[],
            )
        if disposition in {"none", "mobile_store"}:
            return _result(
                status="skipped",
                reason=disposition,
                protocol="browser",
                request=[],
            )
        if disposition != "cancel":
            raise RuntimeError("浏览器套餐状态证据不足，未执行取消")

        registration._safe_get(
            driver,
            _BILLING_URL,
            timeout=45,
            attempts=2,
            accept_hosts=("chatgpt.com",),
            script_timeout=35,
        )
        _open_cancellation_entry(driver)

        # Install immediately before the irreversible confirmation click.  The
        # hook stores only path/method/status and never observes headers/body.
        driver.execute_script(_INSTALL_REQUEST_CAPTURE_JS)
        _click_exact_action(
            driver,
            _CANCEL_CONFIRM_TEXTS,
            timeout=30,
            stage="最终取消确认",
        )
        time.sleep(2.0)
        request_records = _sanitize_cancel_requests(driver.execute_script(_READ_REQUEST_CAPTURE_JS))

        # A precise Manage/Cancel action may lead to the hosted billing portal.
        # Return to ChatGPT before the same-origin accounts/check confirmation.
        if not _is_chatgpt_origin(getattr(driver, "current_url", "")):
            registration._safe_get(
                driver,
                "https://chatgpt.com/",
                timeout=45,
                attempts=2,
                accept_hosts=("chatgpt.com",),
                script_timeout=35,
            )

        verified = _verify_browser_cancellation(driver, new_access_token, account_id)
        if not _is_cancel_confirmed(verified):
            raise RuntimeError("取消后复查未观察到 will_renew=false 或 cancels_at")
        return _result(
            status="success",
            reason="cancel_confirmed",
            protocol="ui",
            request=request_records,
        )
    except Exception as exc:
        # Selenium errors may echo command arguments.  Re-raise only a scrubbed
        # message so the freshly issued token cannot enter a result or DB error.
        safe = _redact(
            f"{type(exc).__name__}: {exc}",
            (email, password, totp_secret, new_access_token),
        )
        raise RuntimeError(safe) from None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                logger.warning("[Subscription] driver 关闭异常: %s", _redact(exc, (email, password, totp_secret)))
        if client is not None:
            try:
                client.cleanup_profile(opened)
            except Exception as exc:
                logger.warning("[Subscription] Roxy profile 清理异常: %s", _redact(exc, (email, password, totp_secret)))


def _process_account_subscription_cancel(*, account_id: int, email: str, trigger: str) -> dict:
    account = db.get_account(account_id) or {}
    if not account:
        result = _result(
            status="failed",
            reason="account_missing",
            protocol="protocol",
            error="账号已删除或不存在",
        )
        _persist_cancel_result(account_id, email, result)
        return result

    stored_email = str(account.get("email") or email or "").strip()
    access_token = str(account.get("access_token") or "").strip()
    password, totp_secret = _stored_login_credentials(account)
    secrets = (stored_email, access_token, password, totp_secret)

    with _redacted_helper_logs(secrets) as scrubber:
        for secret in secrets:
            _cancel_log_add_secret(secret)
        _cancel_log(
            f"开始处理 account_id={account_id} trigger={str(trigger or 'manual')[:40]}"
        )
        logger.info(
            "[Subscription] 开始处理 account_id=%s email=%s trigger=%s",
            account_id,
            _mask_email(stored_email),
            str(trigger or "manual")[:40],
        )
        if _is_mobile_purchase_origin(_purchase_origin(account)):
            result = _result(
                status="skipped",
                reason="mobile_store",
                protocol="mobile_store",
                request=[],
            )
            _persist_cancel_result(account_id, stored_email, result)
            return result

        plan: dict | None = None
        protocol_cancel: dict | None = None
        refreshed_access_token = False
        strict_protocol_after_refresh = False
        protocol_issue = "账号缺少 access_token"
        plan_proxies: list[str] = []
        plan_attempts = 0
        if not access_token:
            strict_protocol_after_refresh = True
            try:
                access_token = _refresh_access_token_via_password_totp(
                    account_id=account_id,
                    email=stored_email,
                    password=password,
                    totp_secret=totp_secret,
                    redactor=scrubber,
                )
                secrets = (*secrets, access_token)
                refreshed_access_token = True
            except Exception as exc:
                error = _redact(f"{type(exc).__name__}: {exc}", secrets)
                _cancel_log(
                    f"缺少 AT 且 Roxy 密码+2FA 登录失败 type={type(exc).__name__}",
                    level="ERROR",
                )
                result = _result(
                    status="failed",
                    reason="protocol_refresh_failed",
                    protocol="protocol",
                    error=error,
                )
                _persist_cancel_result(account_id, stored_email, result)
                return result

        if access_token:
            _cancel_log("正在通过协议查询套餐和订阅状态")
            try:
                plan_proxies = _protocol_proxy_candidates()
                plan, plan_attempts = _query_plan_with_proxy_rotation(
                    access_token,
                    proxies=plan_proxies,
                    secrets=secrets,
                )
            except Exception as exc:
                protocol_issue = _redact(f"{type(exc).__name__}: {exc}", secrets)
                logger.warning(
                    "[Subscription] 协议套餐查询异常 account_id=%s error=%s",
                    account_id,
                    protocol_issue,
                )

        if plan is not None and _needs_access_token_refresh(plan):
            strict_protocol_after_refresh = True
            if not refreshed_access_token:
                try:
                    access_token = _refresh_access_token_via_password_totp(
                        account_id=account_id,
                        email=stored_email,
                        password=password,
                        totp_secret=totp_secret,
                        redactor=scrubber,
                    )
                    secrets = (*secrets, access_token)
                    refreshed_access_token = True
                    _cancel_log("正在使用最新 AT 重新查询套餐和订阅状态")
                    if plan_attempts < _PROTOCOL_MAX_PROXY_ATTEMPTS:
                        plan, plan_attempts = _query_plan_with_proxy_rotation(
                            access_token,
                            proxies=plan_proxies[plan_attempts:],
                            attempt_offset=plan_attempts,
                            secrets=secrets,
                        )
                    else:
                        plan = {
                            "ok": False,
                            "checked_at": datetime.now().isoformat(timespec="seconds"),
                            "attempt_count": plan_attempts,
                            "max_attempts": _PROTOCOL_MAX_PROXY_ATTEMPTS,
                            "error": f"协议套餐查询已达到 {_PROTOCOL_MAX_PROXY_ATTEMPTS} 次上限",
                        }
                except Exception as exc:
                    protocol_issue = _redact(f"{type(exc).__name__}: {exc}", secrets)
                    _cancel_log(
                        f"Roxy 密码+2FA 登录失败 type={type(exc).__name__}",
                        level="ERROR",
                    )
                    result = _result(
                        status="failed",
                        reason="protocol_refresh_failed",
                        protocol="protocol",
                        error=protocol_issue,
                    )
                    _persist_cancel_result(account_id, stored_email, result)
                    return result

        if plan is not None:
            db.update_account_plan_check(acc_id=account_id, result=plan)
            if plan.get("ok"):
                disposition = _subscription_disposition(plan)
                _cancel_log(f"订阅状态查询完成 disposition={disposition}")
                if disposition == "already_cancelled":
                    result = _result(
                        status="success",
                        reason=disposition,
                        protocol="protocol",
                        request=[],
                    )
                    _with_attempt_meta(result, plan)
                    _persist_cancel_result(account_id, stored_email, result)
                    return result
                if disposition in {"none", "mobile_store"}:
                    result = _result(
                        status="skipped",
                        reason=disposition,
                        protocol="mobile_store" if disposition == "mobile_store" else "protocol",
                        request=[],
                    )
                    _with_attempt_meta(result, plan)
                    _persist_cancel_result(account_id, stored_email, result)
                    return result
                if disposition == "cancel":
                    _cancel_log("开始发送协议取消订阅请求，并等待协议复查")
                    try:
                        cancel_proxies = _protocol_proxy_candidates()
                        protocol_cancel, cancel_attempts, cancel_verification_only = _cancel_with_proxy_rotation(
                            access_token,
                            expected_email=stored_email,
                            proxies=cancel_proxies,
                            secrets=secrets,
                        )
                    except Exception as exc:
                        cancel_proxies = []
                        cancel_attempts = 0
                        cancel_verification_only = False
                        protocol_cancel = {
                            "ok": False,
                            "reason": "protocol_cancel_exception",
                            "error": _redact(f"{type(exc).__name__}: {exc}", secrets),
                        }
                    if _needs_access_token_refresh(protocol_cancel):
                        strict_protocol_after_refresh = True
                        if not refreshed_access_token:
                            try:
                                access_token = _refresh_access_token_via_password_totp(
                                    account_id=account_id,
                                    email=stored_email,
                                    password=password,
                                    totp_secret=totp_secret,
                                    redactor=scrubber,
                                )
                                secrets = (*secrets, access_token)
                                refreshed_access_token = True
                                _cancel_log("使用最新 AT 重试协议取消，并重新复查订阅状态")
                                if cancel_attempts < _PROTOCOL_MAX_PROXY_ATTEMPTS:
                                    protocol_cancel, cancel_attempts, cancel_verification_only = _cancel_with_proxy_rotation(
                                        access_token,
                                        expected_email=stored_email,
                                        proxies=cancel_proxies[cancel_attempts:],
                                        attempt_offset=cancel_attempts,
                                        verification_only=cancel_verification_only,
                                        secrets=secrets,
                                    )
                                else:
                                    protocol_cancel = {
                                        **protocol_cancel,
                                        "ok": False,
                                        "reason": "protocol_cancel_failed",
                                        "attempt_count": cancel_attempts,
                                        "max_attempts": _PROTOCOL_MAX_PROXY_ATTEMPTS,
                                        "error": f"协议取消已达到 {_PROTOCOL_MAX_PROXY_ATTEMPTS} 次上限",
                                    }
                            except Exception as exc:
                                protocol_cancel = {
                                    "ok": False,
                                    "reason": "protocol_refresh_or_retry_failed",
                                    "error": _redact(f"{type(exc).__name__}: {exc}", secrets),
                                }
                                _cancel_log(
                                    f"AT 刷新或协议重试失败 type={type(exc).__name__}",
                                    level="ERROR",
                                )
                    if isinstance(protocol_cancel, dict):
                        protocol_cancel = _redact_payload(protocol_cancel, secrets)
                    else:
                        protocol_cancel = {
                            "ok": False,
                            "reason": "protocol_cancel_invalid",
                            "error": "协议取消返回格式异常",
                        }
                    after_plan = protocol_cancel.get("after") or protocol_cancel.get("before")
                    if isinstance(after_plan, dict) and after_plan.get("ok"):
                        db.update_account_plan_check(acc_id=account_id, result=after_plan)
                    cancel_reason = str(protocol_cancel.get("reason") or "")
                    terminal_protocol_result = (
                        bool(protocol_cancel.get("ok"))
                        and cancel_reason in {
                            "cancel_confirmed",
                            "already_cancelled",
                            "none",
                            "mobile_store",
                        }
                        and (
                            cancel_reason != "cancel_confirmed"
                            or protocol_cancel.get("confirmed") is True
                        )
                    )
                    if terminal_protocol_result:
                        result = _result(
                            status=(
                                "success"
                                if cancel_reason in {"cancel_confirmed", "already_cancelled"}
                                else "skipped"
                            ),
                            reason=cancel_reason,
                            protocol=("mobile_store" if cancel_reason == "mobile_store" else "protocol"),
                            request=[],
                        )
                        _with_attempt_meta(result, protocol_cancel)
                        _cancel_log(
                            "协议复查完成 "
                            f"confirmed={bool(protocol_cancel.get('confirmed', True))} "
                            f"reason={cancel_reason}"
                        )
                        _persist_cancel_result(account_id, stored_email, result)
                        return result
                    if strict_protocol_after_refresh:
                        _cancel_log(
                            "刷新 AT 后协议复查未确认 "
                            f"reason={cancel_reason[:64] or 'unknown'}",
                            level="ERROR",
                        )
                    else:
                        _cancel_log(
                            "协议取消未确认，任务按协议失败结束，不进入浏览器兜底 "
                            f"reason={cancel_reason[:64] or 'unknown'}",
                            level="ERROR",
                        )
                    protocol_issue = _protocol_failure_detail(
                        "cancel",
                        protocol_cancel,
                        secrets=secrets,
                    )
                elif disposition == "unknown":
                    protocol_issue = "协议套餐状态不足，未执行取消"
                else:
                    protocol_issue = "协议套餐状态证据不足"
            else:
                protocol_issue = _protocol_failure_detail(
                    "query",
                    plan,
                    secrets=secrets,
                )

        # The current protocol response may be unavailable or omit purchase
        # origin.  A previously persisted mobile-store origin is enough to
        # keep this task out of the web cancellation flow.
        fresh_origin = _purchase_origin(plan) if isinstance(plan, dict) and plan.get("ok") else ""
        cached_origin = _purchase_origin(account)
        if not fresh_origin and _is_mobile_purchase_origin(cached_origin):
            result = _result(
                status="skipped",
                reason="mobile_store",
                protocol="mobile_store",
                request=[],
            )
            _persist_cancel_result(account_id, stored_email, result)
            return result

        error = _redact(
            protocol_issue or "协议查询、取消或复查未完成",
            secrets,
        )
        failure_reason = (
            "protocol_cancel_failed"
            if isinstance(plan, dict)
            and plan.get("ok")
            and _subscription_disposition(plan) == "cancel"
            else "protocol_query_failed"
        )
        _cancel_log(
            "协议链路未完成，任务按失败结束；未进入浏览器兜底",
            level="ERROR",
        )
        result = _result(
            status="failed",
            reason=failure_reason,
            protocol="protocol",
            error=error,
        )
        _with_attempt_meta(
            result,
            protocol_cancel if failure_reason == "protocol_cancel_failed" else plan,
        )
        _persist_cancel_result(account_id, stored_email, result)
        return result


def _run_subscription_cancel(*, account_id: int, email: str, trigger: str) -> dict:
    _set_cancelling(email, True)
    try:
        try:
            account_for_log = db.get_account(account_id) or {}
        except Exception:
            account_for_log = {}
        log_secrets = (
            email,
            account_for_log.get("email"),
            account_for_log.get("access_token"),
            account_for_log.get("chatgpt_password"),
            account_for_log.get("password"),
            account_for_log.get("totp_secret"),
        )
        with _cancel_log_session(email, log_secrets):
            _cancel_log(
                f"销套餐任务开始 account_id={account_id} trigger={str(trigger or 'manual')[:40]}"
            )
            try:
                if not db.mark_account_subscription_cancel_running(account_id, protocol="protocol"):
                    result = _result(
                        status="failed",
                        reason="claim_lost",
                        protocol="protocol",
                        error="账号已删除或取消任务状态已重置",
                    )
                    _cancel_log("任务占用状态已丢失，停止执行", level="ERROR")
                    return result
                return _process_account_subscription_cancel(
                    account_id=account_id,
                    email=email,
                    trigger=trigger,
                )
            except Exception as exc:
                state = getattr(_CANCEL_LOG_LOCAL, "state", None)
                active_secrets = (
                    state.get("secrets")
                    if isinstance(state, dict)
                    else log_secrets
                )
                error = _redact(f"{type(exc).__name__}: {exc}", active_secrets)
                result = _result(
                    status="failed",
                    reason="worker_failed",
                    protocol="worker",
                    error=error,
                )
                _cancel_log(
                    f"后台任务异常 type={type(exc).__name__}",
                    level="ERROR",
                )
                try:
                    _persist_cancel_result(account_id, email, result)
                except Exception:
                    logger.error("[Subscription] 任务失败且状态写回异常 account_id=%s", account_id)
                logger.warning("[Subscription] 后台任务异常 account_id=%s error=%s", account_id, error)
                return result
    finally:
        _set_cancelling(email, False)
        _QUEUE_SLOTS.release()


def _fail_queued_submission(*, account_id: int, email: str, exc: Exception) -> str:
    """Release a claimed queue item when the batch executor rejects it."""
    error = f"订阅取消入队失败: {type(exc).__name__}: {str(exc)[:500]}"
    error = _redact(error, (email,), limit=800)
    _set_cancelling(email, False)
    _QUEUE_SLOTS.release()
    try:
        db.update_account_subscription_cancel(
            acc_id=account_id,
            email=email,
            status="failed",
            error=error,
            protocol="queue",
            outcome="queue_submit_failed",
            message=error,
        )
    except Exception:
        logger.error("[Subscription] 批次任务入队失败且状态写回异常 account_id=%s", account_id)
    return error


class SubscriptionCancelBatch:
    """Schedule one UI batch without letting it exceed its chosen concurrency."""

    def __init__(self, concurrency: int):
        self.concurrency = concurrency
        self._pending: deque[tuple[int, str, str]] = deque()
        self._active = 0
        self._sealed = False
        self._lock = threading.RLock()

    def submit(self, *, account_id: int, email: str, trigger: str) -> str | None:
        task = (account_id, email, trigger)
        with self._lock:
            if self._sealed:
                return _fail_queued_submission(
                    account_id=account_id,
                    email=email,
                    exc=RuntimeError("取消套餐批次已经关闭"),
                )
            self._pending.append(task)
            return self._pump_locked(focus=task)

    def seal(self) -> None:
        """Prevent further additions once the HTTP request has queued the batch."""
        with self._lock:
            self._sealed = True

    def _pump_locked(self, *, focus: tuple[int, str, str] | None = None) -> str | None:
        focus_error = None
        while self._active < self.concurrency and self._pending:
            task = self._pending.popleft()
            account_id, email, trigger = task
            try:
                future = _BATCH_EXECUTOR.submit(
                    _run_subscription_cancel,
                    account_id=account_id,
                    email=email,
                    trigger=trigger,
                )
            except Exception as exc:
                error = _fail_queued_submission(
                    account_id=account_id,
                    email=email,
                    exc=exc,
                )
                if task is focus:
                    focus_error = error
                continue
            self._active += 1
            future.add_done_callback(self._completed)
        return focus_error

    def _completed(self, _future) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._pump_locked()


def create_subscription_cancel_batch(concurrency: int = _WORKERS) -> SubscriptionCancelBatch:
    if type(concurrency) is not int:
        raise ValueError("concurrency 必须是整数")
    if not 1 <= concurrency <= _MAX_BATCH_CONCURRENCY:
        raise ValueError(f"concurrency 必须在 1 到 {_MAX_BATCH_CONCURRENCY} 之间")
    return SubscriptionCancelBatch(concurrency)


def enqueue_account_subscription_cancel(
    account_id: int,
    email: str,
    trigger: str = "manual",
    batch: SubscriptionCancelBatch | None = None,
) -> dict:
    """Queue one account for subscription cancellation using two workers."""
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return {"accepted": False, "busy": False, "error": "account_id 格式错误"}
    email = str(email or "").strip()
    trigger = str(trigger or "manual").strip() or "manual"
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {
            "accepted": False,
            "busy": False,
            "queue_full": True,
            "error": "订阅取消队列已满，请稍后重试",
        }

    try:
        claimed = db.claim_account_subscription_cancel(
            acc_id=account_id,
            email=email,
            protocol="protocol",
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        return {
            "accepted": False,
            "busy": False,
            "error": f"取消任务占用失败: {type(exc).__name__}",
        }
    if not claimed:
        _QUEUE_SLOTS.release()
        return {
            "accepted": False,
            "busy": True,
            "error": "该账号正在执行订阅取消任务",
        }

    _set_cancelling(email, True)
    if batch is not None:
        submit_error = batch.submit(
            account_id=account_id,
            email=email,
            trigger=trigger,
        )
        if submit_error:
            return {"accepted": False, "busy": False, "error": submit_error}
        return {
            "accepted": True,
            "busy": False,
            "account_id": account_id,
            "email": email,
            "status": "queued",
            "trigger": trigger,
            "concurrency": batch.concurrency,
        }

    try:
        _EXECUTOR.submit(
            _run_subscription_cancel,
            account_id=account_id,
            email=email,
            trigger=trigger,
        )
    except Exception as exc:
        _set_cancelling(email, False)
        _QUEUE_SLOTS.release()
        error = f"订阅取消入队失败: {type(exc).__name__}"
        db.update_account_subscription_cancel(
            acc_id=account_id,
            email=email,
            status="failed",
            error=error,
            protocol="queue",
        )
        return {"accepted": False, "busy": False, "error": error}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": trigger,
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "default_concurrency": _WORKERS,
        "max_concurrency": _MAX_BATCH_CONCURRENCY,
        "queue_limit": _QUEUE_LIMIT,
    }
