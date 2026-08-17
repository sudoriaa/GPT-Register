# -*- coding: utf-8 -*-
"""
通用 API 取码邮箱客户端。

邮箱池导入格式：
    email----code_url

注册时领取 email；取码时直接 GET code_url，并从响应中提取 6 位验证码。
响应可以是纯文本、HTML 或 JSON，只要其中包含 6 位验证码即可。
"""
import json
import logging
import re
import time
import base64
import html as html_lib
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlunparse

import requests

from config import email as _email_cfg
from core.otp_utils import extract_otp, extract_reset_link

logger = logging.getLogger(__name__)

_CODE_REGEX = re.compile(r"\b(\d{6})\b")
_CONTEXT_WORDS = ("code", "verify", "verification", "验证码", "代码", "确认码", "認証", "コード")
_CONTEXT_CACHE: dict[str, "GenericApiEmailAccount"] = {}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_FILE = _PROJECT_ROOT / "用于注册的API邮箱.txt"

# xbovo iCloud Hide My Email 验证码 API 服务地址（号池格式：邮箱----alias_xxx）
_XBOVO_API_BASE = "https://icloud.xbovo.online"
_YANGYANG_MESSAGES_RE = re.compile(r"/messages/([^/]+)/([^/?#]+)", re.IGNORECASE)

# iCloud HTML 中转页版式 B（icloud-api.top 等）：<div class="card"> 块
#   <div class="card">
#     <div class="fr">ChatGPT <otp_at_tm1_openai_com_xxx@icloud.com></div>
#     <div class="su">ChatGPT の一時的な認証コード</div>
#     <div class="dt">Tue, 04 Aug 2026 06:29:52 +0000</div>
#     <div class="bd">…整封原始 HTML 正文…</div>
#   </div>
# bd 里是整封邮件 HTML（含嵌套 div），非贪婪切到第一个 </div> 会把正文切断，
# 所以块正则吃到 card 双闭合 </div></div>，bd 从起始标记一直取到块末尾。
# 双闭合间允许空白：真实页面 card 之间常有换行缩进（</div>\n</div>），
# 而 lookahead 保证块只在「后面紧跟下一张 card 或 </body>」处闭合，
# 因此 bd 内部嵌套 div 的闭合不会造成误截断。
_RE_CARD_BLOCK = re.compile(
    r'<div class="card">(.*?)</div>\s*</div>\s*(?=<div class="card">|</body>)', re.S
)
_RE_CARD_FR = re.compile(r'<div class="fr">(.*?)</div>', re.S)
_RE_CARD_SU = re.compile(r'<div class="su">(.*?)</div>', re.S)
_RE_CARD_DT = re.compile(r'<div class="dt">(.*?)</div>', re.S)
_BD_START = '<div class="bd">'

# 只认这些发件人（iCloud 中转站会把发件人改写成 noreply_at_tm_openai_com_xxx@icloud.com，
# 所以匹配的是下划线形态，不是正常域名）
_ICLOUD_FROM_HINTS = (
    "openai", "tm_openai", "chatgpt", "auth0", "tm.openai", "chatgpt.com",
)

# 版式 B 发件人里尖括号是没转义的裸字符，去标签前先摘出来保邮箱地址。
_RE_ANGLE_ADDR = re.compile(r"<([^<>\s]*@[^<>\s]*)>")

_YANGYANG_OPENAI_SUBJECT_HINTS = (
    "temporary chatgpt",
    "chatgpt verification code",
    "chatgpt login code",
    "临时 chatgpt",
    "chatgpt 登录代码",
    "chatgpt 验证码",
    "一時的な認証コード",
    "一時ログインコード",
)


class GenericApiMailError(RuntimeError):
    """通用 API 取码邮箱错误。"""


@dataclass
class GenericApiEmailAccount:
    email: str
    code_url: str


def _flatten_json(obj) -> str:
    parts: list[str] = []
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            parts.append(str(x))
    walk(obj)
    return "\n".join(parts)


def _decode_data_uri(text: str) -> str:
    """把 data:text/html;base64,... 正文解码成可抽取 OTP 的 HTML/文本。"""
    if not isinstance(text, str):
        return ""
    if not text.startswith("data:"):
        return text
    try:
        _meta, payload = text.split(",", 1)
    except ValueError:
        return text
    if ";base64" in _meta.lower():
        try:
            return base64.b64decode(payload).decode("utf-8", errors="replace")
        except Exception:
            return text
    try:
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload).decode("utf-8", errors="replace")
    except Exception:
        return text


def _extract_code(text: str) -> str | None:
    """从纯文本/HTML/JSON 文本中提取 6 位 OTP。"""
    if not text:
        return None

    # 兼容 JSON：优先把所有 value 拉平再抽取。
    candidates_text = [_decode_data_uri(text), text]
    try:
        parsed = json.loads(text)
        candidates_text.insert(0, _decode_data_uri(_flatten_json(parsed)))
    except Exception:
        pass

    for body in candidates_text:
        # 复用邮件 OTP 抽取逻辑。
        code = extract_otp({"text": body, "content": body, "subject": body[:200]})
        if code:
            return code

        codes = _CODE_REGEX.findall(body)
        if not codes:
            continue
        lower = body.lower()
        for code in codes:
            idx = lower.find(code)
            window = lower[max(0, idx - 80): idx + 86]
            if any(w.lower() in window for w in _CONTEXT_WORDS):
                return code
        return codes[-1]
    return None


def _extract_yangyang_openai_code(subject: str, body: str) -> str | None:
    """
    yangyang 邮件详情里 OpenAI 模板常混入多个 6 位数字：
    - 202123 / 353740 这类 CSS/模板数字
    - 真正 OTP 在 “Your code is / code:” 附近，通常是正文最后一个业务 6 位数
    所以不能直接复用通用 _extract_code 的“第一个上下文命中”。
    """
    body = _decode_data_uri(body or "")
    subject_l = (subject or "").lower()
    text = "\n".join([subject or "", body])

    # 去掉 style/script，减少 CSS 颜色、宽高等 6 位数字干扰。
    clean = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<script[^>]*>.*?</script>", " ", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"#[0-9a-fA-F]{6}\b", " ", clean)
    clean = re.sub(r"(?:color|background|border|width|height|font-size|line-height)\s*:\s*[^;\"']+", " ", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    codes = _CODE_REGEX.findall(clean)
    if not codes:
        return None

    # 过滤已知模板噪声；保留其它 6 位候选。
    noise = {"000000", "202123", "353740"}
    candidates = [c for c in codes if c not in noise]
    if not candidates:
        candidates = codes

    lower = clean.lower()
    patterns = (
        r"(?:code is|code:|verification code is|login code is|your code is)\D{0,80}(\d{6})",
        r"(?:验证码|驗證碼|登录代码|登入代碼|確認コード|認証コード|ログインコード)\D{0,80}(\d{6})",
        r"(\d{6})\D{0,80}(?:code|验证码|驗證碼|確認コード|認証コード)",
    )
    for pat in patterns:
        matches = re.findall(pat, clean, flags=re.IGNORECASE)
        matches = [m for m in matches if m not in noise]
        if matches:
            return matches[-1]

    # OpenAI 临时代码邮件：清理噪声后最后一个业务 6 位数最稳定。
    if any(h in subject_l for h in _YANGYANG_OPENAI_SUBJECT_HINTS) or "openai" in lower or "chatgpt" in lower:
        return candidates[-1]

    return _extract_code(clean)


def _parse_yangyang_code_url(code_url: str) -> tuple[str, str, str] | None:
    """
    解析 yangyang.website 这类邮箱页面：
        /messages/{token}/{email}
    返回 (origin, token, email)。
    """
    try:
        parsed = urlparse(code_url)
    except Exception:
        return None
    m = _YANGYANG_MESSAGES_RE.search(parsed.path or "")
    if not m:
        return None
    origin = urlunparse((parsed.scheme or "http", parsed.netloc, "", "", "", ""))
    token = unquote(m.group(1))
    email = unquote(m.group(2))
    if not origin or not token or not email:
        return None
    return origin.rstrip("/"), token, email


def _parse_yangyang_ts(value: str | None) -> float | None:
    """解析中转页时间，兼容两种版式：
      · 版式 A 本地时间：'2026-08-02 13:18:53'（naive）
      · 版式 B RFC2822：'Tue, 04 Aug 2026 06:29:52 +0000'（带时区，可能带 ' (UTC)' 后缀）
    """
    if not value:
        return None
    raw = str(value).strip()
    # 版式 B：有星期几/逗号/月份缩写 → 走 RFC2822
    if "," in raw or re.search(r"\b[A-Z][a-z]{2}\b", raw):
        cleaned = re.sub(r"\s*\([A-Za-z]+\)\s*$", "", raw)
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except Exception:
            pass
    return None


def _parse_generic_api_ts(value) -> float | None:
    """解析通用 API 返回的时间字段，兼容 ISO8601/Z 和常见本地时间格式。"""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # 数字时间戳：秒 / 毫秒
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        try:
            ts = float(raw)
            return ts / 1000.0 if ts > 10_000_000_000 else ts
        except Exception:
            return None
    # ISO8601: 2026-08-05T01:10:17.000Z
    try:
        iso = raw
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    except Exception:
        pass
    # 常见字符串格式
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except Exception:
            pass
    return None


def _extract_structured_api_code(text: str, after_ts: float | None = None) -> tuple[str, dict] | None:
    """
    兼容 newzoe 这类直接返回 JSON 的取码接口：
      {"code":"784207","from":"...","subject":"Your temporary ChatGPT login code","time":"2026-08-05T01:10:17.000Z"}

    如果响应里有 time/date/received_at，会按 after_ts 过滤旧码，避免拿到上一次缓存验证码。
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    # 常见字段优先级：code / otp / verification_code；没有再回退从拉平文本提取。
    raw_code = (
        data.get("code")
        or data.get("otp")
        or data.get("verification_code")
        or data.get("verificationCode")
        or data.get("email_code")
        or data.get("emailCode")
    )
    code = None
    if raw_code is not None:
        m = _CODE_REGEX.search(str(raw_code))
        if m:
            code = m.group(1)
    if not code:
        code = _extract_code(_flatten_json(data))
    if not code:
        return None

    ts_raw = (
        data.get("time")
        or data.get("date")
        or data.get("received_at")
        or data.get("receivedAt")
        or data.get("created_at")
        or data.get("createdAt")
        or data.get("timestamp")
    )
    msg_ts = _parse_generic_api_ts(ts_raw)
    if after_ts and msg_ts and msg_ts + 2 < after_ts:
        logger.debug(
            "[GenericAPI] structured API 跳过旧验证码: code=%s ts=%s after=%s subject=%r",
            code,
            ts_raw,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
            str(data.get("subject") or "")[:80],
        )
        return None

    return code, {
        "source": "structured_api",
        "received_at": ts_raw,
        "msg_ts": msg_ts,
        "subject": data.get("subject"),
        "from": data.get("from") or data.get("fromAddress") or data.get("sender"),
    }


def _parse_mail_api_url(code_url: str) -> tuple[str, str, str] | None:
    """
    识别「微信邮箱取件」链接：`/latest?email=...&auth_code=...`（sms.linlinflow 类）。

    真实取码 API 是页面 JS 里的 `/mail-api/{auth_code}/{email}?folder=inbox`（JSON），
    直接 GET /latest 拿到的只是 JS 渲染的初始 HTML（#code-box 显示"未检测到验证码"）。
    返回 (origin, key, email)；不是该类型返回 None。
    """
    try:
        parsed = urlparse(code_url)
    except Exception:
        return None
    if not parsed.hostname:
        return None
    # youyangai 取件页也是 ?email=..&key=.. 形态，但路径是 /pickup，走 /api/messages；
    # 必须排除，避免被本解析器（/mail-api/{key}/{email}）误抢。
    if "pickup" in (parsed.path or "").lower():
        return None
    try:
        from urllib.parse import parse_qsl
        q = {}
        for k, v in parse_qsl(parsed.query):
            q.setdefault(k.lower(), v)
    except Exception:
        return None
    key = q.get("auth_code") or q.get("code") or q.get("key") or q.get("token")
    email = q.get("email") or q.get("mail")
    if not key or not email:
        return None
    origin = urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", ""))
    return origin.rstrip("/"), unquote(key), unquote(email)


def _first_code(value) -> str | None:
    """从任意值里提取 6 位验证码；非 6 位或空返回 None。"""
    if value is None:
        return None
    m = _CODE_REGEX.search(str(value))
    return m.group(1) if m else None


def _fetch_latest_html_page_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
) -> tuple[str, dict] | None:
    """
    解析「服务端直接渲染邮件」的取件页：GET code_url（如 api798.com 的
    /latest?email=..&auth_code=..），邮件 subject + 正文就内嵌在返回的 HTML 里。

    与 youyangai 的区别：没有 /api/messages JSON 接口，/mail-api 也可能 404；
    直接 GET 取件地址本身就能拿到邮件。验证码用通用 _extract_code 抽取。
    """
    try:
        resp = session.get(
            code_url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug("[GenericAPI] latest 页面 HTTP %s: %s", resp.status_code, (resp.text or "")[:160])
            return None
        html = resp.text or ""
    except Exception as exc:
        logger.debug("[GenericAPI] latest 页面读取失败: %s: %s", type(exc).__name__, exc)
        return None
    code = _extract_code(html)
    if not code:
        logger.debug("[GenericAPI] latest 页面未提取到验证码 (len=%s)", len(html))
        return None
    logger.info("[GenericAPI] latest 页面提取到 OTP=%s url=%s", code, code_url[:140])
    return code, {"source": "mail_api_latest_html", "subject": "", "msg_ts": None}


def _fetch_mail_api_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
) -> tuple[str, dict] | None:
    """从微信邮箱取件 JSON API（/mail-api/{key}/{email}?folder=inbox）抽取最新 6 位验证码。

    顶层 code 字段是接口算好的"最新验证码"；messages[] 里每条还有 verification_code，
    按 after_ts 过滤旧码，避免拿到上一次缓存验证码。

    若该站没有 /mail-api JSON 接口（如 api798.com 返回 404），回退解析取件地址本身
    服务端渲染的邮件 HTML（_fetch_latest_html_page_otp）。
    """
    parsed = _parse_mail_api_url(code_url)
    if not parsed:
        return None
    origin, key, email = parsed
    api_url = (
        f"{origin}/mail-api/{quote(key, safe='')}/{quote(email, safe='@._+-')}"
        f"?folder=inbox"
    )
    try:
        resp = session.get(
            api_url,
            headers={**headers, "Accept": "application/json"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug("[GenericAPI] mail-api HTTP %s: %s", resp.status_code, (resp.text or "")[:160])
            return _fetch_latest_html_page_otp(session, code_url, headers, after_ts=after_ts)
        data = json.loads(resp.text or "")
    except Exception as exc:
        logger.debug("[GenericAPI] mail-api 读取失败: %s: %s", type(exc).__name__, exc)
        return _fetch_latest_html_page_otp(session, code_url, headers, after_ts=after_ts)
    if not isinstance(data, dict):
        return _fetch_latest_html_page_otp(session, code_url, headers, after_ts=after_ts)

    # 顶层 code：接口已算好的最新验证码
    code = _first_code(data.get("code") or data.get("verification_code") or data.get("latest_code"))
    msg_ts = _parse_generic_api_ts(data.get("received_at") or data.get("receivedAt") or data.get("date"))
    if code:
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] mail-api 跳过旧码: code=%s ts=%s after=%s",
                code,
                data.get("received_at"),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
            )
            return None
        logger.info(
            "[GenericAPI] mail-api 顶层提取到 OTP=%s ts=%s email=%s",
            code, data.get("received_at"), email,
        )
        return code, {"source": "mail_api", "received_at": data.get("received_at"), "msg_ts": msg_ts}

    # messages[] 兜底：取列表里最新一封含验证码的 OpenAI 邮件
    msgs = data.get("messages") or []
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        msg_ts2 = _parse_generic_api_ts(
            msg.get("received_time") or msg.get("receivedAt") or msg.get("date") or msg.get("smtp_received_at")
        )
        if after_ts and msg_ts2 and msg_ts2 + 2 < after_ts:
            continue
        c = _first_code(msg.get("verification_code") or msg.get("code"))
        if c:
            logger.info(
                "[GenericAPI] mail-api messages 提取到 OTP=%s subject=%r ts=%s",
                c, str(msg.get("subject") or "")[:80], msg.get("received_time"),
            )
            return c, {
                "source": "mail_api_msg",
                "received_at": msg.get("received_time"),
                "subject": msg.get("subject"),
                "msg_ts": msg_ts2,
            }
    return None


def _parse_xbovo_key(code_url: str) -> str | None:
    """
    识别 xbovo iCloud 取件 key（号池格式：邮箱----alias_xxx）。

    其它模式第二段都是 URL（http(s)://...），而 xbovo 第二段是 API Key
    （文档格式 alias_xxx，经 ?key= 或 X-API-Key 传递）。不含 :// 即视为 key。
    """
    text = str(code_url or "").strip()
    if not text or "://" in text:
        return None
    return text


# flysms（https://flysms.xyz/icloud/pickup）专用主机列表。该站与 youyangai 用同款
# #email=..&key=.. fragment，但取码 API 完全不同（header 认证，见 _fetch_flysms_otp），
# 必须专用解析器处理，绝不能落到 youyangai 的 /api/messages。
_FLYSMS_HOSTS = ("flysms.xyz", "flysms.top")


def _is_flysms_host(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return bool(h) and (h in _FLYSMS_HOSTS or h.endswith((".flysms.xyz", ".flysms.top")))


def _parse_youyangai_url(code_url: str) -> tuple[str, str, str] | None:
    """
    识别 youyangai iCloud 取件地址：https://<host>/pickup
    兼容两种参数位置（真实取码 API 都是 GET {origin}/api/messages?email&key）：
      · fragment：#email=..&key=..（ic.youyangai.top 页面 JS 用 #）
      · query：  ?email=..&key=..（部分站点直接放 query）

    路径须含 "pickup" 以与 mail-api（/latest?email&auth_code）区分。
    返回 (origin, email, key)；不是该类型返回 None。
    """
    text = str(code_url or "").strip()
    if "#" not in text and "?" not in text:
        return None
    try:
        from urllib.parse import parse_qsl, unquote as _unq, urlparse, urlunparse
        parsed = urlparse(text)
        if not parsed.hostname:
            return None
        if _is_flysms_host(parsed.hostname):
            return None
        if "pickup" not in (parsed.path or "").lower():
            return None
        params: dict[str, str] = {}
        if parsed.fragment:
            for k, v in parse_qsl(parsed.fragment):
                params.setdefault(k.lower(), v)
        if parsed.query:
            for k, v in parse_qsl(parsed.query):
                params.setdefault(k.lower(), v)
        email = params.get("email") or params.get("mail") or ""
        key = params.get("key") or params.get("auth_code") or params.get("code") or ""
        if not email or not key:
            return None
        origin = urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", ""))
        return origin.rstrip("/"), _unq(email), _unq(key)
    except Exception:
        return None


def _parse_flysms_url(code_url: str) -> tuple[str, str, str] | None:
    """
    识别 flysms iCloud 取件地址：https://flysms.xyz/icloud/pickup#email=..&key=..
    （与 youyangai 同款 fragment，但取码 API 不同——fragment 里的 key 是 Bearer token）。

    真实取码 API：
        GET {origin}/icloud/api/pickup/messages?limit=<N>
        Headers: Authorization: Bearer <key> / X-Mailbox-Email: <email>
    返回 (origin, email, key)；不是该类型返回 None。
    """
    text = str(code_url or "").strip()
    if "#" not in text:
        return None
    try:
        from urllib.parse import parse_qsl, unquote as _unq, urlparse, urlunparse
        parsed = urlparse(text)
        if not parsed.hostname or not _is_flysms_host(parsed.hostname):
            return None
        if not parsed.fragment:
            return None
        q = {}
        for k, v in parse_qsl(parsed.fragment):
            q.setdefault(k.lower(), v)
        email = q.get("email") or q.get("mail") or ""
        key = q.get("key") or q.get("auth_code") or q.get("code") or ""
        if not email or not key:
            return None
        origin = urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", ""))
        return origin.rstrip("/"), _unq(email), _unq(key)
    except Exception:
        return None


def _fetch_youyangai_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
) -> tuple[str, dict] | None:
    """
    从 youyangai /api/messages 列表 API 抽取最新 6 位验证码。

    响应 {success, email, messages:[{code, codes, subject, from_email, timestamp, ...}]}，
    每条消息已带解析好的 code（6 位验证码）。按 timestamp 过滤 after_ts 之前的旧码。
    """
    parsed = _parse_youyangai_url(code_url)
    if not parsed:
        return None
    origin, email, key = parsed
    api_url = f"{origin}/api/messages"
    params = {"email": email, "key": key, "force": "1"}
    try:
        resp = session.get(
            api_url,
            params=params,
            headers={**headers, "Accept": "application/json"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug("[GenericAPI] youyangai HTTP %s: %s", resp.status_code, (resp.text or "")[:160])
            return None
        data = json.loads(resp.text or "")
    except Exception as exc:
        logger.debug("[GenericAPI] youyangai 读取失败: %s: %s", type(exc).__name__, exc)
        return None
    if not isinstance(data, dict):
        return None

    msgs = data.get("messages") or []
    if not isinstance(msgs, list):
        return None
    # 列表默认新邮件在前；按时间倒序，只认 OpenAI 系邮件
    items = [m for m in msgs if isinstance(m, dict)]
    items.sort(key=lambda x: float(x.get("timestamp") or 0.0), reverse=True)
    for m in items:
        msg_ts = float(m.get("timestamp") or 0.0)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            continue
        code = _first_code(m.get("code") or m.get("verification_code"))
        if not code:
            codes = m.get("codes")
            if isinstance(codes, list) and codes:
                code = _first_code(codes[0])
        if code:
            logger.info(
                "[GenericAPI] youyangai 提取到 OTP=%s subject=%r ts=%s",
                code, str(m.get("subject") or "")[:80], m.get("date"),
            )
            return code, {
                "source": "youyangai",
                "received_at": m.get("date"),
                "subject": m.get("subject"),
                "msg_ts": msg_ts or None,
            }
    return None


def _mail_items_youyangai(session: requests.Session, code_url: str, headers: dict) -> list[dict]:
    parsed = _parse_youyangai_url(code_url)
    if not parsed:
        return []
    origin, email, key = parsed
    try:
        resp = session.get(
            f"{origin}/api/messages",
            params={"email": email, "key": key, "force": "1"},
            headers={**headers, "Accept": "application/json"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            return []
        data = json.loads(resp.text or "")
    except Exception:
        return []
    out = []
    for m in (data.get("messages") or []):
        if not isinstance(m, dict):
            continue
        subject = str(m.get("subject") or "")
        preview = str(m.get("preview") or m.get("body") or "")
        out.append({
            "subject": subject,
            "text": f"{subject}\n{preview}",
            "received_at": m.get("date"),
            "from": m.get("from_email") or m.get("from"),
            "id": m.get("id"),
        })
    # 最新一封补拉详情拿完整正文（列表 preview 被截断到 ~180 字符）
    if out and out[0].get("id"):
        try:
            mid = str(out[0]["id"])
            resp = session.get(
                f"{origin}/api/message/{mid}",
                params={"email": email, "key": key},
                headers={**headers, "Accept": "application/json"},
                timeout=20,
                verify=False,
            )
            if resp.status_code == 200:
                detail = json.loads(resp.text or "")
                dmsg = detail.get("message") if isinstance(detail, dict) else None
                if isinstance(dmsg, dict):
                    body_text = str(dmsg.get("body_text") or "")
                    if body_text:
                        subject = str(dmsg.get("subject") or out[0].get("subject") or "")
                        out[0]["text"] = f"{subject}\n{body_text}"
                        if dmsg.get("date"):
                            out[0]["received_at"] = dmsg.get("date")
        except Exception:
            pass
    return out


def _fetch_flysms_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
) -> tuple[str, dict] | None:
    """
    从 flysms /icloud/api/pickup/messages 列表 API 抽取最新 6 位验证码。

    认证走请求头（不是 query 参数）：
        Authorization: Bearer <key>
        X-Mailbox-Email: <email>
    响应 {email, scope, revision, messages:[{mailbox, uid, subject, from, to, date,
    preview, hasAttachments}], nextCursor}。验证码从 subject/preview 提取；
    按 date 过滤 after_ts 之前的旧码。
    """
    parsed = _parse_flysms_url(code_url)
    if not parsed:
        return None
    origin, email, key = parsed
    api_url = f"{origin}/icloud/api/pickup/messages"
    params = {"limit": "30"}
    try:
        resp = session.get(
            api_url,
            params=params,
            headers={
                **headers,
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "X-Mailbox-Email": email,
            },
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug("[GenericAPI] flysms HTTP %s: %s", resp.status_code, (resp.text or "")[:160])
            return None
        data = json.loads(resp.text or "")
    except Exception as exc:
        logger.debug("[GenericAPI] flysms 读取失败: %s: %s", type(exc).__name__, exc)
        return None
    if not isinstance(data, dict):
        return None

    msgs = data.get("messages") or []
    if not isinstance(msgs, list):
        return None
    # 列表默认新邮件在前；按时间倒序，只认 OpenAI 系邮件
    items = [m for m in msgs if isinstance(m, dict)]
    items.sort(key=lambda x: float(_parse_generic_api_ts(x.get("date")) or 0.0), reverse=True)
    for m in items:
        msg_ts = _parse_generic_api_ts(m.get("date"))
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            continue
        blob = f"{m.get('subject') or ''}\n{m.get('preview') or ''}"
        code = _extract_code(blob)
        if code:
            logger.info(
                "[GenericAPI] flysms 提取到 OTP=%s subject=%r ts=%s",
                code, str(m.get("subject") or "")[:80], m.get("date"),
            )
            return code, {
                "source": "flysms",
                "received_at": m.get("date"),
                "subject": m.get("subject"),
                "msg_ts": msg_ts or None,
            }
    return None


def _mail_items_flysms(session: requests.Session, code_url: str, headers: dict) -> list[dict]:
    parsed = _parse_flysms_url(code_url)
    if not parsed:
        return []
    origin, email, key = parsed
    try:
        resp = session.get(
            f"{origin}/icloud/api/pickup/messages",
            params={"limit": "30"},
            headers={
                **headers,
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "X-Mailbox-Email": email,
            },
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            return []
        data = json.loads(resp.text or "")
    except Exception:
        return []
    out = []
    for m in (data.get("messages") or []):
        if not isinstance(m, dict):
            continue
        subject = str(m.get("subject") or "")
        preview = str(m.get("preview") or "")
        out.append({
            "subject": subject,
            "text": f"{subject}\n{preview}",
            "received_at": m.get("date"),
            "from": m.get("from"),
            "id": m.get("uid"),
        })
    return out


def _mail_items_mail_api(session: requests.Session, code_url: str, headers: dict) -> list[dict]:
    parsed = _parse_mail_api_url(code_url)
    if not parsed:
        return []
    origin, key, email = parsed
    api_url = (
        f"{origin}/mail-api/{quote(key, safe='')}/{quote(email, safe='@._+-')}"
        f"?folder=inbox"
    )
    try:
        resp = session.get(api_url, headers={**headers, "Accept": "application/json"}, timeout=20, verify=False)
        if resp.status_code != 200:
            return _mail_items_latest_html(session, code_url, headers)
        data = json.loads(resp.text or "")
    except Exception:
        return _mail_items_latest_html(session, code_url, headers)
    out = []
    for m in (data.get("messages") or []):
        if not isinstance(m, dict):
            continue
        subject = str(m.get("subject") or "")
        body = str(m.get("body") or m.get("body_preview") or m.get("text") or "")
        out.append({
            "subject": subject,
            "text": f"{subject}\n{body}",
            "received_at": m.get("received_time") or m.get("date"),
            "from": m.get("from_address") or m.get("from"),
        })
    return out


def _mail_items_latest_html(session: requests.Session, code_url: str, headers: dict) -> list[dict]:
    """服务端渲染邮件页（api798 类）：GET 取件地址本身，从 HTML 里取 subject + 正文。"""
    try:
        resp = session.get(
            code_url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            return []
        html = resp.text or ""
    except Exception:
        return []
    subject_m = re.search(r"<title>([^<]*)</title>", html, flags=re.IGNORECASE)
    subject = _strip_html_fragment(subject_m.group(1) if subject_m else "")
    text = _html_to_plain_text(html)
    if not subject and not text:
        return []
    return [{
        "subject": subject,
        "text": f"{subject}\n{text}",
        "received_at": "",
        "from": "",
    }]


def _mail_items_xbovo(session: requests.Session, code_url: str, headers: dict, email: str = "") -> list[dict]:
    key = _parse_xbovo_key(code_url)
    if not key:
        return []
    params = {"key": key, "limit": "50"}
    if email:
        params["email"] = email
    try:
        resp = session.get(
            f"{_XBOVO_API_BASE}/api/v1/messages",
            params=params,
            headers={**headers, "Accept": "application/json"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            return []
        data = json.loads(resp.text or "")
    except Exception:
        return []
    out = []
    for m in (data.get("messages") or []):
        if not isinstance(m, dict):
            continue
        subject = str(m.get("subject") or "")
        preview = str(m.get("preview") or "")
        out.append({
            "subject": subject,
            "text": f"{subject}\n{preview}",
            "received_at": m.get("date") or m.get("received_at"),
            "from": m.get("from") or m.get("from_address"),
        })
    return out


def _mail_items_yangyang(session: requests.Session, code_url: str, headers: dict) -> list[dict]:
    parsed = _parse_yangyang_code_url(code_url)
    if not parsed:
        return []
    origin, token, email = parsed
    token_q = quote(token, safe="")
    email_q = quote(email, safe="@._+-")
    api_url = f"{origin}/api/messages/{token_q}/{email_q}"
    try:
        resp = session.get(api_url, headers={**headers, "Accept": "application/json"}, timeout=20, verify=False)
        if resp.status_code != 200:
            resp = None
        else:
            data = resp.json()
    except Exception:
        resp = None
        data = None

    out = []
    if resp is not None and isinstance(data, dict):
        for m in (data.get("items") or []):
            if not isinstance(m, dict):
                continue
            subject = str(m.get("subject") or "")
            out.append({
                "subject": subject,
                "text": subject,
                "received_at": m.get("received_at"),
                "from": m.get("from_address") or m.get("from"),
            })
    if out:
        return out

    # JSON 列表接口不可用（如 mail.ai1998.xyz 返回 404）→ 回退 inline HTML 页面解析
    try:
        html_resp = session.get(
            code_url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"},
            timeout=20,
            verify=False,
        )
        if html_resp.status_code == 200:
            html = html_resp.text or ""
            # 版式 A（mail-card / details）与版式 B（card 块）都解析
            cards = re.findall(r"<article\b[^>]*class=[\"'][^\"']*mail-card[^\"']*[\"'][^>]*>(.*?)</article>", html, flags=re.DOTALL | re.IGNORECASE)
            if not cards:
                cards = re.findall(r"<details\b[^>]*>(.*?)</details>", html, flags=re.DOTALL | re.IGNORECASE)
            items_raw = []
            for idx, card in enumerate(cards):
                subject_m = re.search(r"<span\b[^>]*class=[\"'][^\"']*subject[^\"']*[\"'][^>]*>(.*?)</span>", card, flags=re.DOTALL | re.IGNORECASE)
                date_m = re.search(r"<span\b[^>]*class=[\"'][^\"']*date[^\"']*[\"'][^>]*>(.*?)</span>", card, flags=re.DOTALL | re.IGNORECASE)
                body_m = re.search(r"<pre\b[^>]*class=[\"'][^\"']*body[^\"']*[\"'][^>]*>(.*?)</pre>", card, flags=re.DOTALL | re.IGNORECASE)
                if not body_m:
                    body_m = re.search(r"<div\b[^>]*class=[\"'][^\"']*body[^\"']*[\"'][^>]*>(.*?)</div>", card, flags=re.DOTALL | re.IGNORECASE)
                subject = _strip_html_fragment(subject_m.group(1) if subject_m else "")
                received_at = _strip_html_fragment(date_m.group(1) if date_m else "")
                body = _strip_html_fragment(body_m.group(1) if body_m else card)
                items_raw.append({"subject": subject, "body": body, "received_at": received_at})
            if not items_raw:
                items_raw = _parse_card_blocks(html)
            for it in items_raw:
                subject = str(it.get("subject") or "")
                body = str(it.get("body") or "")
                out.append({
                    "subject": subject,
                    "text": f"{subject}\n{body}",
                    "received_at": it.get("received_at"),
                    "from": it.get("from") or "",
                })
    except Exception:
        pass
    return out


def fetch_mail_items_for_url(code_url: str, headers: dict | None = None, email: str = "") -> list[dict]:
    """
    拉取该取件地址的邮件列表（subject + 正文片段），用于关键词检测（如 Plus）。

    返回 [{subject, text, received_at, from}]；识别不到类型或拉取失败返回 []。
    支持 youyangai / mail-api / xbovo / yangyang。xbovo 的 messages 接口需要 email 参数。
    """
    code_url = str(code_url or "").strip()
    if not code_url:
        return []
    hdrs = dict(headers or {})
    hdrs.setdefault("Accept", "application/json,text/plain,*/*")
    hdrs.setdefault("User-Agent", "Mozilla/5.0 (compatible; gpt-register/1.0)")
    session = requests.Session()

    if _parse_flysms_url(code_url):
        return _mail_items_flysms(session, code_url, hdrs)
    if _parse_youyangai_url(code_url):
        return _mail_items_youyangai(session, code_url, hdrs)
    if _parse_mail_api_url(code_url):
        return _mail_items_mail_api(session, code_url, hdrs)
    if _parse_xbovo_key(code_url):
        return _mail_items_xbovo(session, code_url, hdrs, email=email)
    if _parse_yangyang_code_url(code_url):
        return _mail_items_yangyang(session, code_url, hdrs)
    return []


def mail_items_contain_plus(code_url: str, headers: dict | None = None, email: str = "") -> tuple[bool, str, int]:
    """
    检测该取件地址的邮件里是否含 "plus"（不区分大小写，匹配单词）。

    返回 (是否含plus, 命中的最新邮件主题, 已检查邮件数)。拉取失败返回 (False, "", 0)。
    """
    items = fetch_mail_items_for_url(code_url, headers=headers, email=email)
    if not items:
        return False, "", 0
    hit = ""
    for it in items:
        blob = f"{it.get('subject', '')}\n{it.get('text', '')}".lower()
        if "plus" in blob:
            hit = str(it.get("subject") or "") or hit
            return True, hit, len(items)
    return False, "", len(items)


def _fetch_xbovo_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
    allow_stale: bool = False,
) -> tuple[str, dict] | None:
    """
    从 xbovo iCloud Hide My Email API 获取最新验证码。

    接口（公开认证，?key=xxx）：
        GET {base}/api/v1/code?key=alias_xxx[&after=<epoch>]
    响应：
        {"ok":true,"code":"123456","email":"...","fetched_at":"..."}
        没有新码时 code 为空字符串，lookup_status="no_recent_code"。

    allow_stale=True 时不再按 after 过滤，改用 allow_stale=true&max_age_seconds=0，
    取邮箱里任意历史验证码（用于等待新码超时前降级，避免邮箱已有验证码却一直取不到）。
    """
    key = _parse_xbovo_key(code_url)
    if not key:
        return None
    api_url = f"{_XBOVO_API_BASE}/api/v1/code"
    params = {"key": key}
    if after_ts and not allow_stale:
        params["after"] = str(int(after_ts))
    if allow_stale:
        params["allow_stale"] = "true"
        params["max_age_seconds"] = "0"
    try:
        resp = session.get(
            api_url,
            params=params,
            headers={**headers, "Accept": "application/json"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug("[GenericAPI] xbovo HTTP %s: %s", resp.status_code, (resp.text or "")[:160])
            return None
        data = json.loads(resp.text or "")
    except Exception as exc:
        logger.debug("[GenericAPI] xbovo 读取失败: %s: %s", type(exc).__name__, exc)
        return None
    if not isinstance(data, dict):
        return None

    code = _first_code(data.get("code") or data.get("verification_code"))
    if not code:
        return None
    mail = data.get("mail") if isinstance(data.get("mail"), dict) else {}
    msg_ts = _parse_generic_api_ts(
        data.get("fetched_at")
        or mail.get("fetched_at")
        or mail.get("received_at")
        or mail.get("date")
    )
    logger.info(
        "[GenericAPI] xbovo 提取到 OTP=%s email=%s fetched_at=%s%s",
        code, data.get("email"), data.get("fetched_at"), "（stale 降级）" if allow_stale else "",
    )
    return code, {
        "source": "xbovo",
        "received_at": data.get("fetched_at"),
        "subject": mail.get("subject"),
        "msg_ts": msg_ts,
        "stale": bool(allow_stale),
    }


def _fetch_yangyang_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
) -> tuple[str, dict] | None:
    """从 yangyang 邮箱页面的列表 API + 详情 API 中抽取最新 6 位验证码。"""
    parsed = _parse_yangyang_code_url(code_url)
    if not parsed:
        return None
    origin, token, email = parsed
    token_q = quote(token, safe="")
    email_q = quote(email, safe="@._+-")
    api_url = f"{origin}/api/messages/{token_q}/{email_q}"

    items: list[dict] = []
    cursor: str | None = None
    # 一般第一页足够；保守支持最多翻 5 页。
    for _ in range(5):
        url = api_url if not cursor else f"{api_url}?cursor={quote(str(cursor), safe='')}"
        resp = session.get(url, headers={**headers, "Accept": "application/json"}, timeout=20, verify=False)
        if resp.status_code != 200:
            if resp.status_code == 404:
                # 兼容 mail.ai1998.xyz 这类同样是 /messages/{token}/{email}，
                # 但没有 /api/messages，邮件直接内嵌在 HTML 页面中的实现。
                return _fetch_inline_messages_page_otp(
                    session=session,
                    code_url=code_url,
                    headers=headers,
                    after_ts=after_ts,
                )
            logger.debug(f"[GenericAPI] yangyang 邮件列表 HTTP {resp.status_code}: {resp.text[:160]}")
            return None
        data = resp.json()
        page_items = data.get("items") or []
        if isinstance(page_items, list):
            items.extend([x for x in page_items if isinstance(x, dict)])
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = str(data.get("next_cursor"))

    # API 默认新邮件在前；再次按时间倒序，尽量取最新验证码。
    items.sort(key=lambda x: _parse_yangyang_ts(x.get("received_at") or x.get("receivedAt")) or 0, reverse=True)
    for item in items:
        msg_ts_raw = item.get("received_at") or item.get("receivedAt")
        msg_ts = _parse_yangyang_ts(msg_ts_raw)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] yangyang 跳过旧邮件: id=%s ts=%s after=%s subject=%r",
                item.get("id"), msg_ts_raw, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
                item.get("subject") or "",
            )
            continue
        msg_id = item.get("id")
        if not msg_id:
            continue
        detail_url = f"{origin}/message/{quote(str(msg_id), safe='')}/{token_q}/{email_q}"
        try:
            detail_resp = session.get(detail_url, headers={**headers, "Accept": "application/json"}, timeout=20, verify=False)
            if detail_resp.status_code != 200:
                continue
            detail = detail_resp.json()
        except Exception as exc:
            logger.debug(f"[GenericAPI] yangyang 邮件详情读取失败: {type(exc).__name__}: {exc}")
            continue

        raw_body = str(detail.get("body") or "")
        body = _decode_data_uri(raw_body)
        subject = str(detail.get("subject") or item.get("subject") or "")
        text = "\n".join([
            subject,
            str(detail.get("fromAddress") or item.get("from_address") or ""),
            str(detail.get("receivedAt") or item.get("received_at") or ""),
            body,
        ])
        code = _extract_yangyang_openai_code(subject, body)
        if code:
            logger.info(
                f"[GenericAPI] yangyang 页面提取到 OTP={code}, "
                f"mail_id={msg_id}, ts={detail.get('receivedAt') or item.get('received_at')}, subject={subject[:80]!r}"
            )
            return code, {
                "mail_id": msg_id,
                "received_at": detail.get("receivedAt") or item.get("received_at"),
                "subject": subject,
                "msg_ts": msg_ts,
            }
    return None


def _strip_html_fragment(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def _html_to_plain_text(s: str) -> str:
    """把整封邮件 HTML 正文压成纯文本，保留换行结构。

    版式 B 的 bd 是原封不动的邮件 HTML。head/style/script/条件注释里的数字
    （字号、色值、行高）不清掉的话，会被 extract_otp 当成验证码。
    """
    s = re.sub(r"<head[^>]*>.*?</head>", " ", s or "", flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.DOTALL)
    s = re.sub(r"<(br|/p|/div|/tr|/td|/h[1-6])[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _parse_card_blocks(html: str) -> list[dict]:
    """解析 iCloud HTML 中转页版式 B（<div class="card"> 块）。

    与版式 A（mail-card）不同的两个关键点：
      1. bd 里是整封原始 HTML（含嵌套 div），不能非贪婪切到第一个 </div>，
         改为从 '<div class="bd">' 一直吃到块末尾。
      2. 发件人写成 'ChatGPT <xxx@icloud.com>'，尖括号是没转义的裸字符，
         去标签前先摘出来保邮箱地址（发件人校验要用）。
    """
    if not html or 'class="card"' not in html:
        return []
    blocks = _RE_CARD_BLOCK.findall(html)
    if not blocks:
        # 只有一封信时页面收尾结构可能不同，退化成整页当一个 card。
        idx = html.find('<div class="card">')
        if idx != -1:
            blocks = [html[idx:]]
    out: list[dict] = []
    for i, block in enumerate(blocks):
        fr_m = _RE_CARD_FR.search(block)
        su_m = _RE_CARD_SU.search(block)
        dt_m = _RE_CARD_DT.search(block)
        bi = block.find(_BD_START)
        body_html = block[bi + len(_BD_START):] if bi != -1 else ""
        # 发件人里的邮箱是没转义的裸尖括号 <xxx@icloud.com>，先摘出来再去标签，
        # 否则会被当成 HTML 标签整段删掉，只剩名字，发件人校验就失效了。
        sender_raw = fr_m.group(1) if fr_m else ""
        sender = _strip_html_fragment(_RE_ANGLE_ADDR.sub(r" \1 ", sender_raw))
        received_at = _strip_html_fragment(dt_m.group(1) if dt_m else "")
        out.append({
            "mail_id": f"card-{i}",
            "subject": _strip_html_fragment(su_m.group(1) if su_m else ""),
            "received_at": received_at,
            "from": sender,
            "body": _html_to_plain_text(body_html),
            "msg_ts": _parse_yangyang_ts(received_at) or 0.0,
        })
    return out


def _looks_like_icloud_openai(item: dict) -> bool:
    """iCloud 中转页发件人/主题校验：只认 OpenAI 系邮件，避免从通知里硬抠码。"""
    blob = f"{item.get('from', '')} {item.get('subject', '')}".lower()
    return any(h in blob for h in _ICLOUD_FROM_HINTS)


def _fetch_inline_messages_page_otp(
    *,
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
) -> tuple[str, dict] | None:
    """解析无 JSON API、直接把邮件卡片渲染在 HTML 里的 /messages 页面。

    ⚠️ 中转站默认只给最新一封，但 OpenAI 的验证码邮件往往不是最新一封
    （实测最新那封是 "New sign-in" 通知，无码，验证码排在更靠后）。
    所以拉页面时附加 all=1&n=20 参数请求全部邮件 —— 各家中转都会忽略
    自己不认识的参数，一起带上无需探测。
    """
    try:
        url = code_url
        try:
            from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
            parts = urlsplit(code_url)
            q = dict(parse_qsl(parts.query))
            q["all"] = "1"
            q.setdefault("n", "20")
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment)
            )
        except Exception:
            pass
        resp = session.get(
            url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug("[GenericAPI] inline messages 页面 HTTP %s: %s", resp.status_code, (resp.text or "")[:160])
            return None
        html = resp.text or ""
    except Exception as exc:
        logger.debug("[GenericAPI] inline messages 页面读取失败: %s: %s", type(exc).__name__, exc)
        return None

    cards = re.findall(r"<article\b[^>]*class=[\"'][^\"']*mail-card[^\"']*[\"'][^>]*>(.*?)</article>", html, flags=re.DOTALL | re.IGNORECASE)
    # 没有 article 时退一步按 details 分块，避免 class 名细微变化。
    if not cards:
        cards = re.findall(r"<details\b[^>]*>(.*?)</details>", html, flags=re.DOTALL | re.IGNORECASE)

    items: list[dict] = []
    for idx, card in enumerate(cards):
        subject_m = re.search(r"<span\b[^>]*class=[\"'][^\"']*subject[^\"']*[\"'][^>]*>(.*?)</span>", card, flags=re.DOTALL | re.IGNORECASE)
        date_m = re.search(r"<span\b[^>]*class=[\"'][^\"']*date[^\"']*[\"'][^>]*>(.*?)</span>", card, flags=re.DOTALL | re.IGNORECASE)
        from_m = re.search(r"<div\b[^>]*class=[\"'][^\"']*meta[^\"']*[\"'][^>]*>(.*?)</div>", card, flags=re.DOTALL | re.IGNORECASE)
        body_m = re.search(r"<pre\b[^>]*class=[\"'][^\"']*body[^\"']*[\"'][^>]*>(.*?)</pre>", card, flags=re.DOTALL | re.IGNORECASE)
        if not body_m:
            body_m = re.search(r"<div\b[^>]*class=[\"'][^\"']*body[^\"']*[\"'][^>]*>(.*?)</div>", card, flags=re.DOTALL | re.IGNORECASE)

        subject = _strip_html_fragment(subject_m.group(1) if subject_m else "")
        received_at = _strip_html_fragment(date_m.group(1) if date_m else "")
        from_addr = _strip_html_fragment(from_m.group(1) if from_m else "")
        body = _strip_html_fragment(body_m.group(1) if body_m else card)
        msg_ts = _parse_yangyang_ts(received_at)
        items.append({
            "mail_id": f"inline-{idx}",
            "subject": subject,
            "received_at": received_at,
            "from": from_addr,
            "body": body,
            "msg_ts": msg_ts or 0.0,
        })

    # 版式 A（mail-card/details）没解析到任何卡片 → 尝试版式 B
    # （iCloud 中转 <div class="card"> + fr/su/dt/bd）
    if not items:
        items = _parse_card_blocks(html)

    items.sort(key=lambda x: float(x.get("msg_ts") or 0.0), reverse=True)
    for item in items:
        msg_ts = float(item.get("msg_ts") or 0.0)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] inline messages 跳过旧邮件: id=%s ts=%s after=%s subject=%r",
                item.get("mail_id"), item.get("received_at"),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
                item.get("subject") or "",
            )
            continue
        # 版式 B（iCloud 中转 card 块）做发件人校验，避免把 "New sign-in" 等
        # 无码通知当成验证码邮件（版式 A 保持宽松，兼容现有 yangyang 页面）。
        if str(item.get("mail_id") or "").startswith("card-") and not _looks_like_icloud_openai(item):
            logger.debug(
                "[GenericAPI] inline card 跳过非 OpenAI 邮件: from=%r",
                str(item.get("from") or "")[:60],
            )
            continue
        code = _extract_yangyang_openai_code(str(item.get("subject") or ""), str(item.get("body") or ""))
        if code:
            logger.info(
                "[GenericAPI] inline messages 页面提取到 OTP=%s, mail_id=%s, ts=%s, subject=%r",
                code, item.get("mail_id"), item.get("received_at"), str(item.get("subject") or "")[:80],
            )
            return code, {
                "mail_id": item.get("mail_id"),
                "received_at": item.get("received_at"),
                "subject": item.get("subject"),
                "msg_ts": msg_ts,
            }
    return None


def pick_account() -> GenericApiEmailAccount:
    """领取一个可用通用 API 邮箱。"""
    from core.db import claim_next_generic_api_email, generic_api_email_pool_summary

    inserted, skipped = import_from_file()
    if inserted:
        logger.info(f"[GenericAPI] 已自动从 {_ACCOUNTS_FILE.name} 导入 {inserted} 个邮箱（跳过 {skipped} 个）")

    row = claim_next_generic_api_email()
    if row is None:
        summary = generic_api_email_pool_summary()
        raise GenericApiMailError(
            f"通用 API 邮箱池没有可用账号: {summary}. 请在 WebUI 邮箱池导入：邮箱----取码地址"
        )
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[account.email] = account
    logger.info(f"[GenericAPI] 选中邮箱: {account.email}（DB id={row.get('id')}）")
    return account


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    """从文本文件导入通用 API 邮箱，每行：email----code_url 或 email====code_url。"""
    from core.db import import_generic_api_emails
    p = Path(path) if path else _ACCOUNTS_FILE
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    if not p.exists():
        return 0, 0
    records = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----") if "----" in line else line.split("====")
        parts = [x.strip() for x in parts]
        if len(parts) < 2:
            continue
        records.append({"email": parts[0], "code_url": parts[1]})
    return import_generic_api_emails(records)


def get_account_context(email: str) -> GenericApiEmailAccount | None:
    if email in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[email]
    from core.db import get_generic_api_email_by_email
    row = get_generic_api_email_by_email(email)
    if row is None:
        return None
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[email] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_generic_api_email
    release_generic_api_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(email, None)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询该邮箱配置的 code_url，直到提取到 6 位验证码或超时。

    settle 机制：首次拿到验证码后不立刻返回，而是继续等 OTP_SETTLE_SECONDS 秒。
    如果期间取码地址返回了不同验证码，则替换候选并重置 settle 倒计时；
    连续 settle 秒没有变化后才返回，避免取到接口缓存中的旧码。
    """
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
    }
    last_error = ""
    best_otp: str | None = None
    best_seen_at: float = 0.0
    settle_until: float | None = None
    logger.info(
        f"[GenericAPI] 开始轮询取码地址: {email}，"
        f"最长 {max_wait or _email_cfg.OTP_MAX_WAIT}s, settle={settle}s"
    )
    # flysms 优先：它的 fragment 与 youyangai 同款，若放后面会被 youyangai 贪吃匹配。
    is_flysms = _parse_flysms_url(account.code_url) is not None
    is_yangyang = (not is_flysms) and _parse_yangyang_code_url(account.code_url) is not None
    is_mail_api = (not is_flysms and not is_yangyang) and _parse_mail_api_url(account.code_url) is not None
    is_xbovo = (not is_flysms and not is_yangyang and not is_mail_api) and _parse_xbovo_key(account.code_url) is not None
    is_youyangai = (not is_flysms and not is_yangyang and not is_mail_api and not is_xbovo) and _parse_youyangai_url(account.code_url) is not None

    while time.time() < deadline:
        try:
            session = requests.Session()
            yy_result = _fetch_yangyang_otp(session, account.code_url, headers, after_ts=after_ts) if is_yangyang else None
            if yy_result:
                code, yy_meta = yy_result
                now_seen = time.time()
                if not best_otp:
                    best_otp = code
                    best_seen_at = now_seen
                    settle_until = now_seen + settle
                    logger.info(
                        f"[GenericAPI] 首次锁定 OTP={code}, source=yangyang mail_id={yy_meta.get('mail_id')} ts={yy_meta.get('received_at')}, "
                        f"等 {settle}s 看取码接口是否出现更新验证码..."
                    )
                elif code != best_otp:
                    logger.info(
                        f"[GenericAPI] 发现更新 OTP={code}, source=yangyang mail_id={yy_meta.get('mail_id')} ts={yy_meta.get('received_at')}，"
                        f"替换之前的 {best_otp}, 重置 settle 计时"
                    )
                    best_otp = code
                    best_seen_at = now_seen
                    settle_until = now_seen + settle
                else:
                    logger.debug(f"[GenericAPI] 取码接口仍返回候选 OTP={best_otp}")
                resp = None
                text = ""
            elif is_mail_api:
                mail_result = _fetch_mail_api_otp(session, account.code_url, headers, after_ts=after_ts)
                if mail_result:
                    code, ma_meta = mail_result
                    now_seen = time.time()
                    if not best_otp:
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        logger.info(
                            f"[GenericAPI] 首次锁定 OTP={code}, source={ma_meta.get('source')} "
                            f"ts={ma_meta.get('received_at')} subject={str(ma_meta.get('subject') or '')[:80]!r}, "
                            f"等 {settle}s 看取码接口是否出现更新验证码..."
                        )
                    elif code != best_otp:
                        logger.info(
                            f"[GenericAPI] 发现更新 OTP={code}, source={ma_meta.get('source')} "
                            f"ts={ma_meta.get('received_at')} subject={str(ma_meta.get('subject') or '')[:80]!r}，"
                            f"替换之前的 {best_otp}, 重置 settle 计时"
                        )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                    else:
                        logger.debug(f"[GenericAPI] mail-api 仍返回候选 OTP={best_otp}")
                else:
                    last_error = "mail-api 尚未返回新的 6 位验证码"
                resp = None
                text = ""
            elif is_xbovo:
                # 接近超时（剩余不足 15s）且一直没等到新码时，降级取邮箱里已有的验证码，
                # 避免「邮箱里其实有一封验证码邮件，但 after 过滤把它当成旧码」导致取不到。
                stale_degrade = (deadline - time.time()) < 15
                xb_result = _fetch_xbovo_otp(
                    session, account.code_url, headers,
                    after_ts=after_ts, allow_stale=stale_degrade,
                )
                if xb_result:
                    code, xb_meta = xb_result
                    if xb_meta.get("stale"):
                        logger.info(
                            f"[GenericAPI] xbovo 降级取到邮箱已有验证码 OTP={code} "
                            f"ts={xb_meta.get('received_at')}，直接返回"
                        )
                        return code
                    now_seen = time.time()
                    if not best_otp:
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        logger.info(
                            f"[GenericAPI] 首次锁定 OTP={code}, source=xbovo "
                            f"ts={xb_meta.get('received_at')} subject={str(xb_meta.get('subject') or '')[:80]!r}, "
                            f"等 {settle}s 看取码接口是否出现更新验证码..."
                        )
                    elif code != best_otp:
                        logger.info(
                            f"[GenericAPI] 发现更新 OTP={code}, source=xbovo "
                            f"ts={xb_meta.get('received_at')} subject={str(xb_meta.get('subject') or '')[:80]!r}，"
                            f"替换之前的 {best_otp}, 重置 settle 计时"
                        )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                    else:
                        logger.debug(f"[GenericAPI] xbovo 仍返回候选 OTP={best_otp}")
                else:
                    last_error = "xbovo 尚未返回新的 6 位验证码" + ("（已尝试降级取邮箱已有码）" if stale_degrade else "")
                resp = None
                text = ""
            elif is_flysms:
                fl_result = _fetch_flysms_otp(session, account.code_url, headers, after_ts=after_ts)
                if fl_result:
                    code, fl_meta = fl_result
                    now_seen = time.time()
                    if not best_otp:
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        logger.info(
                            f"[GenericAPI] 首次锁定 OTP={code}, source=flysms "
                            f"ts={fl_meta.get('received_at')} subject={str(fl_meta.get('subject') or '')[:80]!r}, "
                            f"等 {settle}s 看取码接口是否出现更新验证码..."
                        )
                    elif code != best_otp:
                        logger.info(
                            f"[GenericAPI] 发现更新 OTP={code}, source=flysms "
                            f"ts={fl_meta.get('received_at')} subject={str(fl_meta.get('subject') or '')[:80]!r}，"
                            f"替换之前的 {best_otp}, 重置 settle 计时"
                        )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                    else:
                        logger.debug(f"[GenericAPI] flysms 仍返回候选 OTP={best_otp}")
                else:
                    last_error = "flysms 尚未返回新的 6 位验证码"
                resp = None
                text = ""
            elif is_youyangai:
                yy2_result = _fetch_youyangai_otp(session, account.code_url, headers, after_ts=after_ts)
                if yy2_result:
                    code, yy2_meta = yy2_result
                    now_seen = time.time()
                    if not best_otp:
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        logger.info(
                            f"[GenericAPI] 首次锁定 OTP={code}, source=youyangai "
                            f"ts={yy2_meta.get('received_at')} subject={str(yy2_meta.get('subject') or '')[:80]!r}, "
                            f"等 {settle}s 看取码接口是否出现更新验证码..."
                        )
                    elif code != best_otp:
                        logger.info(
                            f"[GenericAPI] 发现更新 OTP={code}, source=youyangai "
                            f"ts={yy2_meta.get('received_at')} subject={str(yy2_meta.get('subject') or '')[:80]!r}，"
                            f"替换之前的 {best_otp}, 重置 settle 计时"
                        )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                    else:
                        logger.debug(f"[GenericAPI] youyangai 仍返回候选 OTP={best_otp}")
                else:
                    last_error = "youyangai 尚未返回新的 6 位验证码"
                resp = None
                text = ""
            else:
                if is_yangyang:
                    last_error = "yangyang 列表中尚未出现 after_ts 之后的新验证码邮件"
                    resp = None
                    text = ""
                else:
                    resp = session.get(account.code_url, headers=headers, timeout=20, verify=False)
                    text = resp.text or ""
            if resp is None:
                pass
            elif resp.status_code == 200:
                structured = _extract_structured_api_code(text, after_ts=after_ts)
                structured_meta = structured[1] if structured else {}
                code = structured[0] if structured else _extract_code(text)
                if code:
                    now_seen = time.time()
                    if not best_otp:
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                        if structured_meta:
                            logger.info(
                                f"[GenericAPI] 首次锁定 OTP={code}, source=structured_api "
                                f"ts={structured_meta.get('received_at')} subject={str(structured_meta.get('subject') or '')[:80]!r}, "
                                f"等 {settle}s 看取码接口是否出现更新验证码..."
                            )
                        else:
                            logger.info(
                                f"[GenericAPI] 首次锁定 OTP={code}, "
                                f"等 {settle}s 看取码接口是否出现更新验证码..."
                            )
                    elif code != best_otp:
                        if structured_meta:
                            logger.info(
                                f"[GenericAPI] 发现更新 OTP={code}, source=structured_api "
                                f"ts={structured_meta.get('received_at')} subject={str(structured_meta.get('subject') or '')[:80]!r}，"
                                f"替换之前的 {best_otp}, 重置 settle 计时"
                            )
                        else:
                            logger.info(
                                f"[GenericAPI] 发现更新 OTP={code}，"
                                f"替换之前的 {best_otp}, 重置 settle 计时"
                            )
                        best_otp = code
                        best_seen_at = now_seen
                        settle_until = now_seen + settle
                    else:
                        logger.debug(f"[GenericAPI] 取码接口仍返回候选 OTP={best_otp}")
                else:
                    last_error = f"HTTP 200 但未提取到 6 位验证码，响应预览: {text[:160]}"
            else:
                last_error = f"HTTP {resp.status_code}: {text[:160]}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info(
                f"[GenericAPI] settle 完成，返回 OTP={best_otp}, "
                f"候选锁定时间={time.strftime('%H:%M:%S', time.localtime(best_seen_at))}"
            )
            return best_otp

        remaining = int(deadline - now)
        if best_otp and settle_until is not None:
            logger.info(
                f"[GenericAPI] 已锁定候选 OTP={best_otp}，等 settle 中"
                f"（剩余 settle ~{max(0, int(settle_until - now))}s, 总剩余 {remaining}s）..."
            )
        else:
            logger.info(
                f"[GenericAPI] 暂未从取码接口拿到验证码，"
                f"{interval}s 后重试（剩余 {remaining}s）..."
            )
        time.sleep(interval)

    if best_otp:
        logger.warning(f"[GenericAPI] 总超时但已有候选，返回 OTP={best_otp}")
        return best_otp

    raise GenericApiMailError(f"等待通用 API 验证码超时: {email}; {last_error}")


def fetch_latest_reset_link(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询取码地址，对最新 OpenAI 邮件抽取密码重置链接（2FA 补跑顺带设密码用）。

    复用 fetch_mail_items_for_url 建 item 列表（youyangai 最新一封带完整 body_text，
    mail-api/xbovo 是 subject+preview），逐个跑 extract_reset_link；普通 OTP 邮件
    返回 None 会被跳过。子 provider 拉不到正文 URL 时抛 GenericApiMailError，
    由上层按非致命处理。after_ts 过滤用 _parse_generic_api_ts/_parse_yangyang_ts
    尽力解析，避免捡到上一次补跑留下的过期重置邮件。
    """
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    headers = {
        "Accept": "application/json,text,plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
    }
    last_error = ""
    logger.info(
        f"[GenericAPI] 开始轮询重置链接: {email}，最长 {max_wait or _email_cfg.OTP_MAX_WAIT}s"
    )
    while time.time() < deadline:
        try:
            items = fetch_mail_items_for_url(account.code_url, headers=headers, email=email)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(interval)
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if after_ts is not None:
                received = it.get("received_at")
                ts = _parse_generic_api_ts(received) or _parse_yangyang_ts(received) or 0.0
                if ts and ts + 2 < after_ts:
                    continue
            link = extract_reset_link(it)
            if link:
                logger.info(
                    "[GenericAPI] 提取到重置链接 subject=%r",
                    str(it.get("subject") or "")[:80],
                )
                return link
        last_error = "取码地址尚未出现密码重置链接"
        logger.info(f"[GenericAPI] 暂未取到重置链接，{interval}s 后重试...")
        time.sleep(interval)

    raise GenericApiMailError(f"等待密码重置链接超时: {email}; {last_error}")
