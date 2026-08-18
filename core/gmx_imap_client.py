# -*- coding: utf-8 -*-
"""Low-level GMX/Caramail IMAP transport.

GMX-family mailboxes use the same account pool format as the existing
``mailcom`` source (``address----password``), but their mobile OAuth endpoint
is not the mail.com endpoint.  This module reads those accounts through the
standard IMAP-over-TLS service instead.

``list_messages`` returns the same normalized message dictionaries as
``mailcom_client._list_messages``.  The mail.com client can therefore route a
GMX-family address here while retaining its existing OTP/reset-link polling
and settle state machine::

    messages = list_messages(email, password, after_ts)

No credentials or messages are persisted by this module.
"""
from __future__ import annotations

import email as email_lib
import hashlib
import imaplib
import logging
import re
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)


class GmxImapError(RuntimeError):
    """GMX IMAP 登录、连接或取信失败。"""


# GMX has regional domains which all use the same IMAP service.  Caramail is
# operated by the same 1&1 Mail & Media backend and accepts GMX IMAP login.
# Common regional domains are listed for discovery/documentation.  The routing
# predicate also accepts future ``gmx.*`` regional domains because they share
# the same service endpoint.
GMX_DOMAINS = frozenset(
    {
        "gmx.com",
        "gmx.net",
        "gmx.de",
        "gmx.at",
        "gmx.ch",
        "gmx.fr",
        "gmx.es",
        "gmx.co.uk",
        "gmx.us",
        "gmx.biz",
        "gmx.li",
        "gmx.lv",
        "gmx.pl",
        "gmx.ru",
        "gmx.tm",
        "gmx.com.mx",
        "caramail.com",
        "caramail.fr",
        "caramail.net",
    }
)

GMX_DEFAULT_IMAP_HOSTS = ("imap.gmx.com", "imap.gmx.net")
GMX_DEFAULT_IMAP_PORT = 993
GMX_DEFAULT_IMAP_FOLDER = "INBOX"
GMX_DEFAULT_MESSAGE_LIMIT = 25
GMX_DEFAULT_TIMEOUT = 25

_EMAIL_LOCKS: dict[str, threading.RLock] = {}
_EMAIL_LOCKS_GUARD = threading.Lock()
# A successful regional endpoint is reused on subsequent polls.  If it goes
# stale, _connect removes it and walks the default list again.
_HOST_CACHE: dict[str, tuple[str, int, bool]] = {}


def _cache_key(address: str) -> str:
    return str(address or "").strip().lower()


def is_gmx_email(address: str) -> bool:
    """Return whether *address* belongs to a known GMX/Caramail domain."""
    value = str(address or "").strip().lower()
    if "@" not in value:
        return False
    local, domain = value.rsplit("@", 1)
    domain = domain.rstrip(".")
    return bool(local) and (domain in GMX_DOMAINS or domain.startswith("gmx."))


def _endpoint_candidates(email: str) -> list[tuple[str, int, bool]]:
    """Return GMX SSL endpoints, preferring the last successful host."""
    out = [(host, GMX_DEFAULT_IMAP_PORT, True) for host in GMX_DEFAULT_IMAP_HOSTS]
    cached = _HOST_CACHE.get(_cache_key(email))
    if cached and cached in out:
        out.remove(cached)
        out.insert(0, cached)
    return out


def clear_host_cache(email: str | None = None) -> None:
    """Clear the successful-endpoint cache (mainly for tests/reloads)."""
    if email is None:
        _HOST_CACHE.clear()
        return
    key = _cache_key(email)
    _HOST_CACHE.pop(key, None)


def _email_lock(email: str) -> threading.RLock:
    key = _cache_key(email)
    with _EMAIL_LOCKS_GUARD:
        return _EMAIL_LOCKS.setdefault(key, threading.RLock())


def _connect(email: str, password: str):
    """Try each GMX endpoint and return ``(connection, folder)``.

    The successful TLS endpoint is cached per address; GMX uses SSL on port
    993.
    """
    folder = GMX_DEFAULT_IMAP_FOLDER
    timeout = GMX_DEFAULT_TIMEOUT
    endpoints = _endpoint_candidates(email)
    if not endpoints:
        raise GmxImapError("GMX IMAP 未配置可用服务器")

    errors: list[str] = []
    cache_key = _cache_key(email)
    for host, port, use_ssl in endpoints:
        conn = None
        try:
            cls = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
            try:
                conn = cls(host, port, timeout=timeout)
            except TypeError:
                # A few test doubles and older Python builds do not expose the
                # timeout keyword; retain compatibility with their constructor.
                conn = cls(host, port)
            typ, data = conn.login(email, password)
            if typ != "OK":
                raise imaplib.IMAP4.error(data or "LOGIN failed")
            _HOST_CACHE[cache_key] = (host, port, use_ssl)
            logger.debug("[GMX IMAP] 已连接 %s:%s (%s)", host, port, "SSL" if use_ssl else "plain")
            return conn, folder
        except Exception as exc:
            # Never include the password in diagnostics.  IMAP responses can
            # contain arbitrary server text, so cap the message length.
            # Some IMAP implementations echo the supplied login value in a
            # diagnostic.  Scrub it before it reaches logs or the propagated
            # error so a pool password never appears in task output.
            detail = str(exc)
            if str(password):
                detail = detail.replace(str(password), "***")
            detail = detail[:160]
            errors.append(f"{host}:{port} {type(exc).__name__}: {detail}")
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
            # A cached endpoint may have become unavailable; force the next
            # poll to walk the complete list rather than pinning the failure.
            if _HOST_CACHE.get(cache_key) == (host, port, use_ssl):
                _HOST_CACHE.pop(cache_key, None)

    raise GmxImapError(f"GMX IMAP 登录失败 {email}: {'; '.join(errors)}")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return "".join(
            part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else str(part)
            for part, charset in decode_header(value)
        )
    except Exception:
        return str(value)


def _part_text(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        # A non-encoded text payload can be a str (notably malformed but common
        # forwarded messages).  Keep it instead of silently dropping the body.
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        return payload.decode("utf-8", errors="replace")


def _parse_message(raw_bytes: bytes, *, message_id: str | None = None, ts: float | None = None) -> dict | None:
    """Normalize an RFC822 message for :mod:`core.otp_utils`.

    The returned shape deliberately includes both ``text``/``html`` and the
    common API aliases ``bodyText``/``bodyHtml``.  This lets the existing OTP
    and reset-link extractors work without provider-specific branches.
    """
    if not raw_bytes:
        return None
    try:
        msg = email_lib.message_from_bytes(raw_bytes)
    except Exception:
        return None

    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = (msg,)
    for part in parts:
        content_type = str(part.get_content_type() or "").lower()
        if content_type == "text/plain":
            body = _part_text(part)
            if body:
                text_parts.append(body)
        elif content_type == "text/html":
            body = _part_text(part)
            if body:
                html_parts.append(body)

    date_header = _decode(msg.get("Date") or "")
    parsed_ts = ts
    if parsed_ts is None and date_header:
        try:
            parsed_ts = parsedate_to_datetime(date_header).timestamp()
        except Exception:
            parsed_ts = None
    item = {
        "id": str(message_id or _decode(msg.get("Message-ID") or "")),
        "from": _decode(msg.get("From") or ""),
        "to": _decode(msg.get("To") or ""),
        "cc": _decode(msg.get("Cc") or ""),
        "subject": _decode(msg.get("Subject") or ""),
        "date": date_header,
        "text": "\n".join(text_parts),
        "html": "\n".join(html_parts),
        "ts": float(parsed_ts) if parsed_ts is not None else 0.0,
    }
    item["bodyText"] = item["text"]
    item["bodyHtml"] = item["html"]
    return item


def _extract_fetch_payload(fdata) -> tuple[bytes, bytes]:
    """Extract ``(metadata, RFC822 bytes)`` from imaplib FETCH data."""
    metadata = b""
    raw = b""
    if not fdata:
        return metadata, raw
    for entry in fdata:
        if isinstance(entry, tuple):
            head = entry[0] if len(entry) > 0 else b""
            body = entry[1] if len(entry) > 1 else b""
            if isinstance(head, str):
                head = head.encode()
            if isinstance(body, str):
                body = body.encode()
            if isinstance(head, bytes) and head:
                metadata = head
            if isinstance(body, bytes) and body:
                raw = body
        elif isinstance(entry, bytes):
            # Closing ``b')'`` entries are harmless; prefer the largest byte
            # payload as the actual message body.
            if len(entry) > len(raw) and not entry.strip() in {b")", b"}"}:
                raw = entry
    return metadata, raw


def _internal_date(metadata: bytes) -> tuple[float | None, str]:
    if not metadata:
        return None, ""
    match = re.search(rb"INTERNALDATE\s+\"([^\"]+)\"", metadata, re.IGNORECASE)
    if not match:
        return None, ""
    text = match.group(1).decode("utf-8", "replace")
    try:
        return parsedate_to_datetime(text).timestamp(), text
    except Exception:
        return None, text


def _uid_from_metadata(metadata: bytes) -> str:
    """Return the stable IMAP UID from one FETCH metadata line."""
    if not metadata:
        return ""
    match = re.search(rb"(?:^|[ (])UID\s+(\d+)(?:[ )]|$)", metadata, re.IGNORECASE)
    return match.group(1).decode("ascii", "replace") if match else ""


def _fetch_messages_since(
    conn,
    folder: str,
    since_ts: float,
    *,
    message_limit: int | None = None,
) -> list[dict]:
    """Fetch and normalize messages newer than *since_ts* from ``folder``."""
    try:
        limit = int(message_limit if message_limit is not None else GMX_DEFAULT_MESSAGE_LIMIT)
    except (TypeError, ValueError):
        limit = GMX_DEFAULT_MESSAGE_LIMIT
    limit = max(1, min(100, limit))
    try:
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            return []
        if float(since_ts) <= 0:
            typ, data = conn.search(None, "ALL")
        else:
            search_date = (
                datetime.fromtimestamp(float(since_ts), dt_timezone.utc) - timedelta(days=1)
            ).strftime("%d-%b-%Y")
            typ, data = conn.search(None, "SINCE", search_date)
        if typ != "OK" or not data:
            return []
        ids = (data[0] or b"").split()
    except Exception as exc:
        raise GmxImapError(f"GMX IMAP 搜索邮件失败: {type(exc).__name__}: {str(exc)[:160]}") from exc

    # IMAP sequence numbers are ordered oldest→newest.  Fetch newest first and
    # cap work for large inboxes; final output remains chronological for the
    # caller's ranking logic.
    ids = ids[-limit:]
    out: list[dict] = []
    for msg_id in reversed(ids):
        try:
            typ, fdata = conn.fetch(msg_id, "(UID INTERNALDATE BODY.PEEK[])")
            if typ != "OK":
                continue
            metadata, raw = _extract_fetch_payload(fdata)
            if not raw:
                continue
            ts, _ = _internal_date(metadata)
            uid = _uid_from_metadata(metadata)
            item = _parse_message(
                raw,
                message_id=f"uid:{uid}" if uid else None,
                ts=ts,
            )
            if item is None:
                continue
            # UID is the preferred stable identity.  Message-ID is used by
            # _parse_message when UID is absent; malformed mail can omit both,
            # so retain a deterministic content hash as the final fallback.
            if not item.get("id"):
                item["id"] = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            # An old message can appear in the one-day date window; filter with
            # the server InternalDate when available.
            if ts is not None and ts < float(since_ts):
                continue
            out.append(item)
        except Exception as exc:
            logger.debug("[GMX IMAP] 读取消息 %s 失败: %s", msg_id, exc)
    # Match maildotcom-sdk: newest message first.  The shared mail.com polling
    # state machine uses list order as the tie-breaker when timestamps match.
    out.sort(key=lambda item: float(item.get("ts") or 0.0), reverse=True)
    return out


def _list_messages(
    email: str,
    password: str,
    after_ts: float,
    *,
    message_limit: int | None = None,
) -> list[dict]:
    """Open one short-lived IMAP session and return normalized messages."""
    with _email_lock(email):
        conn = None
        try:
            conn, folder = _connect(email, password)
            return _fetch_messages_since(
                conn,
                folder,
                after_ts,
                message_limit=message_limit,
            )
        except GmxImapError:
            raise
        except Exception as exc:
            raise GmxImapError(
                f"GMX IMAP 取信失败 {email}: {type(exc).__name__}: {str(exc)[:160]}"
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass


def list_messages(
    email: str,
    password: str,
    after_ts: float,
    *,
    message_limit: int | None = None,
) -> list[dict]:
    """Public low-level interface returning normalized GMX messages.

    Each message contains ``id``, ``from``, ``to``, ``subject``, ``text``,
    ``html`` and ``ts``.  It is intentionally compatible with
    ``mailcom_client._list_messages`` so that mail.com can dispatch by domain
    and retain its existing OTP/reset-link polling state machine.
    """
    target = str(email or "").strip()
    if not target:
        raise GmxImapError("GMX IMAP 取信缺少邮箱地址")
    if not password:
        raise GmxImapError(f"GMX IMAP 邮箱密码缺失: {target}")
    try:
        after = float(after_ts)
    except (TypeError, ValueError) as exc:
        raise GmxImapError(f"GMX IMAP after_ts 无效: {after_ts!r}") from exc
    return _list_messages(
        target,
        str(password),
        after,
        message_limit=message_limit,
    )


__all__ = [
    "GMX_DOMAINS",
    "GMX_DEFAULT_IMAP_HOSTS",
    "GmxImapError",
    "clear_host_cache",
    "is_gmx_email",
    "list_messages",
]
