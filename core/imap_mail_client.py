# -*- coding: utf-8 -*-
"""IMAP 邮箱（邮箱----密码直连取信）客户端。

适用于标准 IMAP 后端（如 Roundcube Webmail 的后端 119.28.25.51:143）：
用邮箱 + 密码登录 IMAP，轮询 INBOX 里的 OpenAI 验证码邮件并提取 6 位 OTP。

与其它邮箱源一致：
    pick_account()            → 从 imap_pass 池领取一个可用邮箱（标 used）
    fetch_latest_otp(email)   → 轮询取 OTP
    release_account(email)    → 回收/标记状态
    get_account_context(email) → 读取邮箱 + 密码
"""
from __future__ import annotations

import email as email_lib
import imaplib
import logging
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

from core import otp_utils

logger = logging.getLogger(__name__)

_IMAP_DEFAULT_HOST = "119.28.25.51"
_IMAP_DEFAULT_PORT = 143


class ImapMailError(Exception):
    pass


def _imap_settings(email: str = "") -> tuple[str, int, bool, str]:
    """解析 IMAP 连接参数。优先用该邮箱导入时指定的服务商地址（imap_host），
    否则回退全局 IMAP_HOST/IMAP_PORT 配置。imap_host 支持 host 或 host:port。
    """
    from config import email as _email_cfg
    host = str(getattr(_email_cfg, "IMAP_HOST", "") or "").strip() or _IMAP_DEFAULT_HOST
    try:
        port = int(getattr(_email_cfg, "IMAP_PORT", 0) or _IMAP_DEFAULT_PORT)
    except (TypeError, ValueError):
        port = _IMAP_DEFAULT_PORT
    use_ssl = bool(getattr(_email_cfg, "IMAP_USE_SSL", False))
    folder = str(getattr(_email_cfg, "IMAP_FOLDER", "INBOX") or "INBOX")

    if email:
        try:
            from core import db
            row = db.get_imap_email_by_email(email)
            addr = ((row or {}).get("imap_host") or "").strip()
            if addr:
                if ":" in addr:
                    h, _, p = addr.rpartition(":")
                    if h and p.strip().isdigit():
                        host, port = h, int(p.strip())
                    else:
                        host = addr
                else:
                    host = addr
        except Exception:
            pass
    return host, port, use_ssl, folder


def _connect(email: str, password: str):
    """连接 IMAP 并登录，返回 (conn, folder)。失败抛 ImapMailError。"""
    host, port, use_ssl, folder = _imap_settings(email)
    try:
        cls = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
        conn = cls(host, port)
        conn.login(email, password)
        return conn, folder
    except imaplib.IMAP4.error as exc:
        raise ImapMailError(f"IMAP 登录失败 {email}: {str(exc)[:160]}") from exc
    except OSError as exc:
        raise ImapMailError(
            f"IMAP 连接失败 {host}:{port}: {type(exc).__name__}: {str(exc)[:120]}"
        ) from exc


def get_account_context(email: str) -> dict | None:
    """按邮箱从 imap_pass 池读取 {email, password}，未导入返回 None。"""
    from core import db
    row = db.get_imap_email_by_email(email)
    if row is None:
        return None
    return {"email": row["email"], "password": row.get("password") or ""}


def pick_account():
    """领取一个可用 imap_pass 邮箱，返回 {email, password}。"""
    from core import db
    row = db.claim_next_imap_email()
    if row is None:
        raise ImapMailError("imap_pass 邮箱池没有可用邮箱，请先导入")
    return SimpleNamespace(email=row["email"], password=row.get("password") or "")


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core import db
    db.release_imap_email(email, status=status, note=note)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = decode_header(value)
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(str(text))
        return "".join(out)
    except Exception:
        return str(value)


def _parse_message(raw_bytes: bytes) -> dict | None:
    """解析一封 IMAP 邮件为 {subject, from, text, html}。"""
    if not raw_bytes:
        return None
    try:
        msg = email_lib.message_from_bytes(raw_bytes)
    except Exception:
        return None
    subject = _decode(msg.get("Subject") or "")
    from_addr = _decode(msg.get("From") or "")
    text_parts = []
    html_parts = []
    for part in msg.walk():
        ct = (part.get_content_type() or "").lower()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        try:
            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            body = payload.decode("utf-8", errors="replace")
        if ct == "text/plain":
            text_parts.append(body)
        elif ct == "text/html":
            html_parts.append(body)
    return {
        "subject": subject,
        "from": from_addr,
        "text": "\n".join(text_parts),
        "html": "\n".join(html_parts),
    }


def _fetch_messages_since(conn, folder: str, since_ts: float) -> list[dict]:
    """抓取 since_ts 之后的邮件，返回 [{item, ts}]。"""
    conn.select(folder, readonly=True)
    # IMAP SINCE 只按日期；多取一天再用 InternalDate 精确过滤
    from datetime import datetime, timedelta, timezone as dt_timezone
    search_date = (datetime.fromtimestamp(since_ts, dt_timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = conn.search(None, "SINCE", search_date)
    if typ != "OK":
        return []
    ids = (data[0] or b"").split()
    out = []
    for msg_id in ids:
        try:
            typ, fdata = conn.fetch(msg_id, "(INTERNALDATE BODY.PEEK[])")
            if typ != "OK" or not fdata or fdata[0] is None:
                continue
            meta = fdata[0]
            internal = b""
            raw = b""
            if isinstance(meta, tuple):
                parts = meta
                raw = parts[1] if len(parts) > 1 else b""
                head = parts[0] if parts else b""
                # head 形如 b'1 (INTERNALDATE "14-Aug-2026 10:00:00 +0800" BODY[] {123}'
                try:
                    idx = head.lower().find(b"internaldate")
                    if idx >= 0:
                        q1 = head.find(b'"', idx)
                        q2 = head.find(b'"', q1 + 1) if q1 >= 0 else -1
                        if q1 >= 0 and q2 > q1:
                            internal = head[q1 + 1:q2]
                except Exception:
                    pass
            elif isinstance(meta, bytes):
                raw = meta
            if not raw:
                continue
            ts = None
            if internal:
                try:
                    ts = parsedate_to_datetime(internal.decode("utf-8", "replace")).timestamp()
                except Exception:
                    ts = None
            item = _parse_message(raw)
            if item:
                out.append({"item": item, "ts": ts})
        except Exception as exc:
            logger.debug("[IMAP] 读取消息 %s 失败: %s", msg_id, exc)
    return out


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    extractor=None,
) -> str:
    """轮询 IMAP 收件箱，返回 after_ts 之后最新 OpenAI 6 位验证码。"""
    from config import email as _email_cfg
    target = str(email or "").strip()
    if not target:
        raise ImapMailError("IMAP 取码缺少邮箱地址")

    account = get_account_context(target)
    if not account or not account.get("password"):
        raise ImapMailError(f"imap_pass 邮箱上下文缺失: {target}（请先导入邮箱----密码）")

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    if poll_interval is not None:
        interval = max(1, int(poll_interval))
    else:
        from config import fast_mode as _fast
        interval = _fast.fast_otp_poll_interval(int(_email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    after = float(after_ts if after_ts is not None else time.time()) - 30
    deadline = time.monotonic() + max(0, wait_seconds)
    extract = extractor or otp_utils.extract_otp

    best_otp: str | None = None
    best_ts = float("-inf")
    settle_until: float | None = None
    last_error = "收件箱为空或尚未出现新的 OpenAI 验证码"

    logger.info("[IMAP] 开始轮询 %s，最长 %ss", target, wait_seconds)

    while time.monotonic() < deadline:
        conn = None
        try:
            conn, folder = _connect(target, account["password"])
            messages = _fetch_messages_since(conn, folder, after)
            logger.debug("[IMAP] %s 本轮收件 %s 封", target, len(messages))
        except ImapMailError as exc:
            last_error = str(exc)
            logger.warning("[IMAP] %s", exc)
            time.sleep(interval)
            continue
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

        for msg in messages:
            ts = msg.get("ts") if msg.get("ts") is not None else 0.0
            if after_ts and ts and ts < after:
                continue
            item = msg["item"]
            if not otp_utils.looks_like_openai_email(item):
                continue
            code = extract(item)
            if not code:
                continue
            if ts > best_ts:
                # 更晚到达的邮件：更新候选并启动 settle 计时（等可能的重发新码）
                best_otp = code
                best_ts = ts
                settle_until = time.monotonic() + settle
                logger.info("[IMAP] 候选 OTP=%s (ts=%s)，settle %ss...", code, ts, settle)
            elif ts == best_ts and code != best_otp:
                # 同一时间戳但验证码不同（重发）：更新候选，不重新延长 settle
                best_otp = code
                logger.info("[IMAP] 同时间戳更新候选 OTP=%s", code)

        if settle_until is not None and time.monotonic() >= settle_until:
            return best_otp
        time.sleep(interval)

    if best_otp:
        return best_otp
    raise ImapMailError(f"{target} 等待 OTP 超时（{wait_seconds}s）: {last_error}")


def fetch_latest_reset_link(email: str, after_ts: float | None = None, **kw) -> str:
    """IMAP 取密码重置链接（2FA 补跑设密码用）。"""
    from config import email as _email_cfg
    target = str(email or "").strip()
    account = get_account_context(target)
    if not account or not account.get("password"):
        raise ImapMailError(f"imap_pass 邮箱上下文缺失: {target}")
    wait_seconds = int(kw.get("max_wait") or _email_cfg.OTP_MAX_WAIT)
    deadline = time.monotonic() + wait_seconds
    after = float(after_ts if after_ts is not None else time.time()) - 30
    while time.monotonic() < deadline:
        conn = None
        try:
            conn, folder = _connect(target, account["password"])
            messages = _fetch_messages_since(conn, folder, after)
        except ImapMailError as exc:
            logger.warning("[IMAP] %s", exc)
            time.sleep(3)
            continue
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
        for msg in messages:
            if after_ts and msg.get("ts") and msg["ts"] < after_ts:
                continue
            item = msg["item"]
            if not otp_utils.looks_like_openai_email(item):
                continue
            link = otp_utils.extract_reset_link(item)
            if link:
                return link
        time.sleep(3)
    raise ImapMailError(f"{target} 等待密码重置链接超时")
