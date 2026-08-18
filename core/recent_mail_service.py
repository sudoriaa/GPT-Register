# -*- coding: utf-8 -*-
"""Read and normalize recent messages for rows in the local email pool.

This module is intentionally read-only: opening the recent-mail viewer never
claims, releases, or otherwise changes an email-pool row.  Provider credentials
stay inside the provider client; callers receive only bounded plain-text mail
fields.
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlsplit

from core import db


SUPPORTED_SOURCES = {
    "outlook",
    "generic_api",
    "imap_pass",
    "mailcom",
    "cloudflare_domain",
}
SOURCE_ALIASES = {
    "gmx": "mailcom",
    "gmx.com": "mailcom",
    "caramail": "mailcom",
    "caramail.com": "mailcom",
    "mail.com": "mailcom",
    "mail_com": "mailcom",
}
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
MAX_SUBJECT_LENGTH = 500
MAX_ADDRESS_LENGTH = 1000
MAX_RECEIVED_AT_LENGTH = 160
MAX_PREVIEW_LENGTH = 1000
MAX_TEXT_LENGTH = 20_000


class RecentMailError(RuntimeError):
    """Base error raised by the recent-mail service."""


class RecentMailValidationError(RecentMailError):
    """The requested source, address, or limit is invalid."""


class RecentMailNotFoundError(RecentMailError):
    """The requested row does not exist in the selected local pool."""


class RecentMailFetchError(RecentMailError):
    """The provider could not read the mailbox."""


class _PlainTextParser(HTMLParser):
    _BREAK_TAGS = {
        "br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3",
        "h4", "h5", "h6", "blockquote", "section", "article", "table",
    }
    _IGNORED_TAGS = {"script", "style", "head", "title", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
        lowered = str(tag or "").lower()
        if lowered in self._IGNORED_TAGS:
            self.ignored_depth += 1
        elif not self.ignored_depth and lowered in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if not self.ignored_depth and str(tag or "").lower() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = str(tag or "").lower()
        if lowered in self._IGNORED_TAGS:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif not self.ignored_depth and lowered in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth and data:
            self.parts.append(data)


def normalize_source(source: object) -> str:
    value = str(source or "").strip().lower()
    value = SOURCE_ALIASES.get(value, value)
    if value not in SUPPORTED_SOURCES:
        raise RecentMailValidationError(
            "source 仅支持 outlook / generic_api / imap_pass / mailcom / cloudflare_domain"
        )
    return value


def normalize_limit(limit: object) -> int:
    if limit in (None, ""):
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise RecentMailValidationError("limit 必须是整数") from exc
    if value < 1:
        raise RecentMailValidationError("limit 必须大于 0")
    return min(MAX_LIMIT, value)


def _pool_row(source: str, email: str) -> dict | None:
    getter = {
        "outlook": db.get_outlook_by_email,
        "generic_api": db.get_generic_api_email_by_email,
        "imap_pass": db.get_imap_email_by_email,
        "mailcom": db.get_mailcom_email_by_email,
        "cloudflare_domain": db.get_domain_email_by_email,
    }[source]
    row = getter(email)
    return dict(row) if isinstance(row, dict) else None


def _secret_values(row: dict) -> tuple[str, ...]:
    values: set[str] = set()
    row_email = str(row.get("email") or "").strip().casefold()
    for key in (
        "password",
        "client_id",
        "clientId",
        "refresh_token",
        "refreshToken",
        "access_token",
        "totp_secret",
        "code_url",
        "copy_line",
        "account_copy_line",
    ):
        value = str(row.get(key) or "").strip()
        if len(value) >= 4:
            values.add(value)

    code_url = str(row.get("code_url") or "").strip()
    if code_url:
        try:
            parsed = urlsplit(code_url)
            for _key, value in parse_qsl(parsed.query, keep_blank_values=False):
                decoded = unquote(str(value or "")).strip()
                if len(decoded) >= 6:
                    values.add(decoded)
            for segment in parsed.path.split("/"):
                decoded = unquote(segment).strip()
                if (
                    len(decoded) >= 12
                    and "@" not in decoded
                    and decoded.casefold() != row_email
                    and decoded.lower() not in {
                    "messages", "message", "mail-api", "api", "latest",
                    }
                ):
                    values.add(decoded)
        except (TypeError, ValueError):
            pass
    return tuple(sorted(values, key=len, reverse=True))


_HTML_TAG_RE = re.compile(
    r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s+[^<>]*?)?\s*/?>|<!--.*?-->|<!DOCTYPE[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{12,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?:\.[A-Za-z0-9_-]{8,})?\b")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _redact_known(value: object, secrets: tuple[str, ...]) -> str:
    text = str(value or "")
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    return text


def _plain_text(value: object) -> str:
    text = str(value or "")
    if _HTML_TAG_RE.search(text):
        parser = _PlainTextParser()
        try:
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _address_text(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(part for part in (_address_text(item) for item in value) if part)
    if isinstance(value, dict):
        nested = (
            value.get("emailAddress")
            or value.get("EmailAddress")
            or value.get("mailbox")
        )
        if isinstance(nested, dict):
            return _address_text(nested)
        address = str(
            value.get("address")
            or value.get("Address")
            or value.get("email")
            or value.get("fromEmail")
            or ""
        ).strip()
        name = str(value.get("name") or value.get("Name") or "").strip()
        if name and address:
            return f"{name} <{address}>"
        return address or name
    return str(value or "").strip()


def _received_at(item: dict) -> str:
    raw = (
        item.get("received_at")
        or item.get("receivedDateTime")
        or item.get("date")
        or item.get("DateTimeReceived")
        or item.get("ReceivedDateTime")
        or ""
    )
    if raw:
        return str(raw)
    ts = item.get("ts")
    try:
        numeric = float(ts)
    except (TypeError, ValueError):
        numeric = 0.0
    if numeric > 0:
        return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return ""


def _body_text(item: dict) -> str:
    candidates = []
    for key in (
        "text", "bodyText", "body_text", "content", "body", "html",
        "bodyHtml", "body_html", "bodyPreview", "preview",
    ):
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("content") or value.get("Content") or ""
        plain = _plain_text(value)
        if plain:
            candidates.append(plain)
    return max(candidates, key=len, default="")


def _normalize_message(item: dict, secrets: tuple[str, ...]) -> dict:
    text = _redact_known(_body_text(item), secrets)[:MAX_TEXT_LENGTH]
    preview_value = item.get("preview") or item.get("bodyPreview") or item.get("body_preview") or ""
    preview = _redact_known(_plain_text(preview_value), secrets)
    if not preview:
        preview = text[:MAX_PREVIEW_LENGTH]
    sender = item.get("fromEmail") or item.get("from") or item.get("From") or item.get("sender") or ""
    recipient = item.get("to") or item.get("To") or item.get("recipients") or ""
    return {
        "subject": _redact_known(_plain_text(item.get("subject") or item.get("Subject") or ""), secrets)[:MAX_SUBJECT_LENGTH],
        "from": _redact_known(_plain_text(_address_text(sender)), secrets)[:MAX_ADDRESS_LENGTH],
        "to": _redact_known(_plain_text(_address_text(recipient)), secrets)[:MAX_ADDRESS_LENGTH],
        "received_at": _redact_known(_plain_text(_received_at(item)), secrets)[:MAX_RECEIVED_AT_LENGTH],
        "preview": preview[:MAX_PREVIEW_LENGTH],
        "text": text,
    }


def _safe_error(exc: BaseException, secrets: tuple[str, ...]) -> str:
    detail = _redact_known(str(exc or ""), secrets)

    def strip_url(match: re.Match) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ").,;]":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parsed = urlsplit(raw)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}?[REDACTED]{trailing}"
        except ValueError:
            return "[REDACTED_URL]"

    detail = _URL_RE.sub(strip_url, detail)
    detail = detail.replace("\r", " ").replace("\n", " ").strip()
    return detail[:240] or type(exc).__name__


def _provider_messages(source: str, email: str, limit: int) -> list[dict]:
    if source == "outlook":
        from core.outlook_client import list_recent_messages
    elif source == "generic_api":
        from core.generic_api_mail_client import list_recent_messages
    elif source == "imap_pass":
        from core.imap_mail_client import list_recent_messages
    elif source == "mailcom":
        from core.mailcom_client import list_recent_messages
    else:
        from core.qqmail_client import list_recent_messages
    rows = list_recent_messages(email, limit=limit)
    return [dict(item) for item in (rows or []) if isinstance(item, dict)]


def fetch_recent_messages(email: object, source: object, limit: object = DEFAULT_LIMIT) -> dict:
    """Return bounded plain-text messages for one local email-pool row."""
    target = str(email or "").strip()
    if not target or "@" not in target:
        raise RecentMailValidationError("email 参数无效")
    normalized_source = normalize_source(source)
    normalized_limit = normalize_limit(limit)
    row = _pool_row(normalized_source, target)
    if row is None:
        raise RecentMailNotFoundError("所选邮箱池中不存在该邮箱")
    canonical_email = str(row.get("email") or target).strip()
    secrets = _secret_values(row)
    try:
        raw_messages = _provider_messages(normalized_source, canonical_email, normalized_limit)
    except Exception as exc:
        detail = _safe_error(exc, secrets)
        raise RecentMailFetchError(f"最近邮件读取失败: {detail}") from exc

    messages = [_normalize_message(item, secrets) for item in raw_messages[:normalized_limit]]
    return {
        "source": normalized_source,
        "email": canonical_email,
        "count": len(messages),
        "messages": messages,
    }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "RecentMailError",
    "RecentMailFetchError",
    "RecentMailNotFoundError",
    "RecentMailValidationError",
    "SUPPORTED_SOURCES",
    "fetch_recent_messages",
    "normalize_limit",
    "normalize_source",
]
