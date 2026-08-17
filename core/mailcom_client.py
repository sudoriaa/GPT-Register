# -*- coding: utf-8 -*-
"""mail.com / GMX / Caramail 共用邮箱池客户端。

mail.com 通过 stdin/stdout JSON bridge 调用 Node SDK；GMX/Caramail 地址按域名
自动切换官方 IMAP SSL。邮箱密码不会进入进程命令行，SDK token session 缓存在
``run/mailcom_sessions``，轮询时可复用登录态。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from core import otp_utils

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BRIDGE_PATH = Path(__file__).with_name("mailcom_bridge.mjs")
_DEFAULT_SESSION_DIR = _PROJECT_ROOT / "run" / "mailcom_sessions"


class MailComError(RuntimeError):
    """mail.com/GMX 登录、取信 bridge 或验证码提取失败。"""


@dataclass(frozen=True)
class MailComAccount:
    email: str
    password: str


_CONTEXT_CACHE: dict[str, MailComAccount] = {}
_EMAIL_LOCKS: dict[str, threading.RLock] = {}
_EMAIL_LOCKS_GUARD = threading.Lock()
_DIRECT_ROUTE_EMAILS: set[str] = set()


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _remember(account: MailComAccount) -> MailComAccount:
    _CONTEXT_CACHE[_cache_key(account.email)] = account
    # 兼容旧改密模块按原始邮箱字符串直接更新缓存。
    _CONTEXT_CACHE[account.email] = account
    return account


def _email_lock(email: str) -> threading.RLock:
    key = _cache_key(email)
    with _EMAIL_LOCKS_GUARD:
        return _EMAIL_LOCKS.setdefault(key, threading.RLock())


def _node_binary() -> str:
    try:
        from config import email as _email_cfg

        configured = str(getattr(_email_cfg, "MAILCOM_NODE_BIN", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return configured
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    raise MailComError("mail.com 取码需要 Node.js 20+；请安装 Node.js 并确保 node 在 PATH 中")


def _session_dir() -> Path:
    try:
        from config import email as _email_cfg

        configured = str(getattr(_email_cfg, "MAILCOM_SESSION_DIR", "") or "").strip()
    except Exception:
        configured = ""
    if not configured:
        return _DEFAULT_SESSION_DIR
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path.resolve()


def _message_limit() -> int:
    try:
        from config import email as _email_cfg

        value = int(getattr(_email_cfg, "MAILCOM_MESSAGE_LIMIT", 25) or 25)
    except (TypeError, ValueError, ImportError):
        value = 25
    return max(1, min(100, value))


def _bridge_timeout() -> int:
    try:
        from config import email as _email_cfg

        value = int(getattr(_email_cfg, "MAILCOM_REQUEST_TIMEOUT", 60) or 60)
    except (TypeError, ValueError, ImportError):
        value = 60
    return max(15, value)


def _run_bridge(payload: dict, timeout: int | None = None) -> dict:
    if not _BRIDGE_PATH.exists():
        raise MailComError(f"mail.com SDK bridge 不存在: {_BRIDGE_PATH}")
    command = [_node_binary(), str(_BRIDGE_PATH)]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=timeout or _bridge_timeout(),
            check=False,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise MailComError(f"mail.com SDK 请求超时（>{exc.timeout}s）") from exc
    except OSError as exc:
        raise MailComError(f"mail.com SDK 启动失败: {type(exc).__name__}: {exc}") from exc

    stdout = str(completed.stdout or "").strip()
    stderr = str(completed.stderr or "").strip()
    if completed.returncode != 0:
        diagnostic = "\n".join(part for part in (stderr, stdout) if part)
        if "maildotcom-sdk" in diagnostic and "ERR_MODULE_NOT_FOUND" in diagnostic:
            detail = "缺少 maildotcom-sdk；请在项目根目录执行 npm install"
        else:
            detail = stderr.splitlines()[-1] if stderr else stdout[:500]
            try:
                parsed_error = json.loads(detail)
                detail = str(parsed_error.get("error") or detail)
            except (TypeError, json.JSONDecodeError, AttributeError):
                pass
        raise MailComError(f"mail.com SDK 调用失败: {detail[:800]}")
    try:
        result = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MailComError(f"mail.com SDK 返回内容不是 JSON: {stdout[:500]}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise MailComError(f"mail.com SDK 返回失败: {str(result)[:500]}")
    return result


def _proxy_route_login_failed(exc: BaseException) -> bool:
    """mail.com 代理出口使 OAuth 上下文失效时，允许一次直连复核。"""
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "android oauth authcode redirect did not include location header",
            "android oauth login redirect did not include location header",
            "support.mail.com/account/login",
            "client network socket disconnected before secure tls connection",
            "econnreset",
            "etimedout",
        )
    )


def _list_messages(email: str, password: str, after_ts: float) -> list[dict]:
    # GMX/Caramail 与 mail.com 同属一套账号体系，但 OAuth 主机不同。继续复用
    # mailcom 邮箱池，只在取信层按域名切到 GMX 官方 IMAP，避免无效 OAuth 重试。
    from core import gmx_imap_client

    if gmx_imap_client.is_gmx_email(email):
        try:
            return gmx_imap_client.list_messages(
                email,
                password,
                after_ts,
                message_limit=_message_limit(),
            )
        except gmx_imap_client.GmxImapError as exc:
            detail = str(exc)
            if str(password):
                detail = detail.replace(str(password), "***")
            raise MailComError(detail) from exc
        except Exception as exc:
            detail = str(exc)
            if str(password):
                detail = detail.replace(str(password), "***")
            detail = detail[:240]
            raise MailComError(
                f"GMX IMAP 取信失败: {type(exc).__name__}: {detail}"
            ) from exc

    with _email_lock(email):
        key = _cache_key(email)
        configured_proxy = _mailcom_proxy()
        prefer_direct = key in _DIRECT_ROUTE_EMAILS
        payload = {
            "action": "list_messages",
            "email": email,
            "password": password,
            "session_dir": str(_session_dir()),
            "proxy": "" if prefer_direct else configured_proxy,
            "after_ts": float(after_ts),
            "amount": _message_limit(),
        }
        try:
            result = _run_bridge(payload)
        except MailComError as proxy_exc:
            if prefer_direct or not configured_proxy or not _proxy_route_login_failed(proxy_exc):
                raise
            logger.warning(
                "[MailCom] 代理 OAuth 路径被 mail.com 退回/断开，"
                "当前邮箱自动切换直连并重建 Session: %s",
                email,
            )
            direct_payload = dict(payload)
            direct_payload["proxy"] = ""
            try:
                result = _run_bridge(direct_payload)
            except MailComError as direct_exc:
                raise MailComError(
                    f"mail.com 代理 OAuth 失败，直连复核也失败: {direct_exc}"
                ) from direct_exc
            _DIRECT_ROUTE_EMAILS.add(key)
            logger.info("[MailCom] 直连 OAuth/Session 已建立，后续取码复用直连: %s", email)
    messages = result.get("messages")
    if not isinstance(messages, list):
        raise MailComError("mail.com SDK 响应缺少 messages 数组")
    return [item for item in messages if isinstance(item, dict)]


def get_account_context(email: str) -> MailComAccount | None:
    key = _cache_key(email)
    cached = _CONTEXT_CACHE.get(key) or _CONTEXT_CACHE.get(str(email or "").strip())
    if cached is not None:
        return cached
    from core import db

    row = db.get_mailcom_email_by_email(email)
    if row is None:
        return None
    return _remember(
        MailComAccount(
            email=str(row.get("email") or "").strip(),
            password=str(row.get("password") or ""),
        )
    )


def pick_account() -> MailComAccount:
    from core import db

    row = db.claim_next_mailcom_email()
    if row is None:
        raise MailComError("mail.com / GMX 邮箱池没有可用邮箱，请先导入 邮箱地址----登录密码")
    account = MailComAccount(
        email=str(row.get("email") or "").strip(),
        password=str(row.get("password") or ""),
    )
    if not account.email or not account.password:
        raise MailComError("mail.com / GMX 邮箱池记录缺少邮箱或登录密码")
    return _remember(account)


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core import db

    db.release_mailcom_email(email, status=status, note=note)
    if status in {"failed", "disabled"}:
        _CONTEXT_CACHE.pop(_cache_key(email), None)
        _CONTEXT_CACHE.pop(str(email or "").strip(), None)
        _DIRECT_ROUTE_EMAILS.discard(_cache_key(email))
        try:
            from core import gmx_imap_client

            gmx_imap_client.clear_host_cache(email)
        except Exception:
            pass


def _message_timestamp(item: dict) -> float | None:
    raw = item.get("ts")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _fetch_latest_value(
    email: str,
    *,
    after_ts: float | None,
    max_wait: int | None,
    poll_interval: int | None,
    settle_seconds: int | None,
    extractor,
    value_label: str,
) -> str:
    from config import email as _email_cfg
    from core.gmx_imap_client import is_gmx_email

    target = str(email or "").strip()
    provider_label = "GMX/Caramail" if is_gmx_email(target) else "mail.com"
    if not target:
        raise MailComError(f"{provider_label} 取{value_label}缺少邮箱地址")
    account = get_account_context(target)
    if account is None or not account.password:
        raise MailComError(f"{provider_label} 邮箱上下文缺失: {target}（请先导入 邮箱地址----登录密码）")

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    requested_interval = int(
        poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL
    )
    # maildotcom-sdk 文档要求轮询间隔不少于 3 秒。
    interval = max(3, requested_interval)
    settle = max(
        0,
        int(
            settle_seconds
            if settle_seconds is not None
            else _email_cfg.OTP_SETTLE_SECONDS
        ),
    )
    # mail.com 的邮件时间来自服务端 epoch；只保留 2 秒时钟容差，避免把本轮
    # 请求前几十秒的旧验证码误当成最新码。未给 after_ts 的兼容入口仍看最近 30 秒。
    after = (
        float(after_ts) - 2.0
        if after_ts is not None
        else time.time() - 30.0
    )
    deadline = time.monotonic() + max(0, wait_seconds)
    best_value: str | None = None
    best_rank: tuple[float, int] | None = None
    best_message_id = ""
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 邮件"
    first_poll = True

    log_tag = "GMX IMAP" if is_gmx_email(target) else "MailCom"
    logger.info("[%s] 开始轮询 %s，最长 %ss", log_tag, target, wait_seconds)
    while first_poll or time.monotonic() < deadline:
        first_poll = False
        try:
            messages = _list_messages(target, account.password, after)
            for index, item in enumerate(messages):
                message_time = _message_timestamp(item)
                if after_ts is not None and message_time is not None and message_time < after:
                    continue
                if not otp_utils.looks_like_openai_email(item):
                    continue
                value = extractor(item)
                if not value:
                    continue
                message_id = str(item.get("id") or "")
                rank = (message_time if message_time is not None else after, -index)
                newer = best_rank is None or rank > best_rank
                changed_same_message = message_id and message_id == best_message_id and value != best_value
                if newer or changed_same_message:
                    best_value = value
                    best_rank = rank
                    best_message_id = message_id
                    settle_until = time.monotonic() + settle
                    logger.info("[%s] 锁定最新%s候选，等待 %ss 确认", log_tag, value_label, settle)
        except MailComError as exc:
            last_error = str(exc)
            logger.warning("[%s] %s", log_tag, exc)

        now = time.monotonic()
        if best_value and settle_until is not None and now >= settle_until:
            return best_value
        remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_value:
        return best_value
    raise MailComError(f"{target} 等待{value_label}超时（{wait_seconds}s）: {last_error}")


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    extractor=None,
) -> str:
    return _fetch_latest_value(
        email,
        after_ts=after_ts,
        max_wait=max_wait,
        poll_interval=poll_interval,
        settle_seconds=settle_seconds,
        extractor=extractor or otp_utils.extract_otp,
        value_label="验证码",
    )


def fetch_latest_reset_link(email: str, after_ts: float | None = None, **kwargs) -> str:
    return _fetch_latest_value(
        email,
        after_ts=after_ts,
        max_wait=kwargs.get("max_wait"),
        poll_interval=kwargs.get("poll_interval"),
        settle_seconds=kwargs.get("settle_seconds"),
        extractor=otp_utils.extract_reset_link,
        value_label="密码重置链接",
    )


# 兼容现有 mail_password_change.py 的 requests 会话入口。密码修改仍由该模块处理；
# 这里的 login 先通过 mobile SDK 验证账号密码，再由其 account.mail.com 表单建立 Web 会话。
def _mailcom_proxy() -> str:
    try:
        from config import email as _email_cfg

        configured = str(getattr(_email_cfg, "MAILCOM_PROXY", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return configured
    try:
        from config import proxy as _proxy_cfg

        pre_proxy = str(getattr(_proxy_cfg, "PROXY_PRE_PROXY", "") or "").strip()
        if pre_proxy:
            return pre_proxy
        return str(getattr(_proxy_cfg, "PROXY", "") or "").strip()
    except Exception:
        return ""


def _http_session(proxy: str = "") -> requests.Session:
    session = requests.Session()
    session.trust_env = not bool(proxy)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


class MailComLightClient:
    def __init__(
        self,
        username: str,
        password: str,
        session: requests.Session | None = None,
    ):
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.session = session or _http_session()
        self.accept_language = "en-US,en;q=0.9"

    def login(self) -> None:
        _list_messages(self.username, self.password, time.time() - 60)
