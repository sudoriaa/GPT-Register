# -*- coding: utf-8 -*-
"""
OTP 检测与抽取通用工具，被 outlook_client（Outlook 邮箱）使用。

要求：
    - 多语言关键字识别（英 / 中 / 日 / 韩）
    - 字段名容错（不同邮件 API 用不同的字段命名约定）
    - 上下文优先：在多个 6 位数中，选择离"验证码"等关键字最近的那个
"""
import html as html_lib
import logging
import re

logger = logging.getLogger(__name__)

_OPENAI_SENDER_HINT = "openai"

# 多语言关键字（用于判断是否是 OpenAI 邮件）
_OPENAI_KEYWORDS = (
    "chatgpt", "openai",
    # 英文
    "verification code", "code is", "your code", "verify your email",
    # 中文
    "代码", "验证码", "确认码",
    # 日文
    "認証コード", "検証コード", "確認コード", "一時検証", "認証",
    # 韩文
    "인증 코드", "확인 코드",
)

# OTP 上下文关键字（用于在多个 6 位数中挑出真正的验证码）
_OTP_CONTEXT_KEYWORDS = (
    "code", "verify", "verification",
    "代码", "验证", "确认",
    "コード", "認証", "検証", "確認",
    "코드", "인증",
)

_OTP_REGEX = re.compile(r"\b(\d{6})\b")


def _get_field(item: dict, *names: str) -> str:
    """
    从邮件 dict 中按顺序尝试多个可能的字段名，返回第一个非空字符串。
    用于兼容不同邮件 API 的字段命名约定（例如 sendEmail / from / fromEmail / from.address）。
    """
    for name in names:
        if "." in name:
            # 支持 "from.emailAddress.address" 这种点路径
            value = item
            for part in name.split("."):
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
            if isinstance(value, str) and value:
                return value
        else:
            value = item.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def looks_like_openai_email(item: dict) -> bool:
    """
    判断邮件是否来自 OpenAI / ChatGPT。多语言、多字段名兼容。

    字段名容错（不同 API 返回风格不一）：
        发件人:  sendEmail / from / fromEmail / from.emailAddress.address
        发件人名:sendName / fromName / from.emailAddress.name
        纯文本:  text / bodyPreview / bodyText
        HTML:    content / body / html / body.content / bodyHtml
    """
    sender = _get_field(item, "sendEmail", "from", "fromEmail", "from.emailAddress.address").lower()
    sender_name = _get_field(item, "sendName", "fromName", "from.emailAddress.name").lower()
    subject = _get_field(item, "subject").lower()
    text = _get_field(item, "text", "bodyPreview", "bodyText").lower()
    content = _get_field(item, "content", "body", "html", "body.content", "bodyHtml").lower()

    if _OPENAI_SENDER_HINT in sender or _OPENAI_SENDER_HINT in sender_name:
        return True

    return any(k in s for s in (subject, text, content) for k in _OPENAI_KEYWORDS)


def extract_otp(item: dict) -> str | None:
    """
    从邮件中抽出 6 位 OTP。

    抽取顺序：
        1. subject（OpenAI 部分邮件直接把 6 位数放在主题里，例 "Your OpenAI code is 525210"）
        2. 纯文本字段（text / bodyPreview / bodyText）
        3. HTML 字段（content / html / body / body.content / bodyHtml，去标签后）

    若 body 中含多个 6 位数，优先选择离 "验证码 / code / 認証" 等关键字最近的那个。
    """
    # 1. 主题里如果直接有 6 位数，最可信
    subject = _get_field(item, "subject")
    if subject:
        codes_in_subject = _OTP_REGEX.findall(subject)
        if len(codes_in_subject) == 1:
            # 主题里恰好只有一个 6 位数，几乎肯定就是 OTP
            return codes_in_subject[0]

    # 2. body 字段
    candidates = [
        ("text", _get_field(item, "text", "bodyPreview", "bodyText")),
        ("html", _get_field(item, "content", "html", "body", "body.content", "bodyHtml")),
    ]

    for kind, body in candidates:
        if not body:
            continue
        # 无论 text 还是 html，都先去 HTML 标签和 style 属性
        # （QQ 邮箱转发的 OpenAI 邮件，text 字段也可能含 HTML）
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        all_codes = _OTP_REGEX.findall(body)
        if not all_codes:
            continue
        body_lower = body.lower()
        # 优先选离上下文关键字最近的 6 位数
        for code in all_codes:
            idx = body_lower.find(code)
            if idx < 0:
                continue
            window = body_lower[max(0, idx - 60): idx + 6 + 60]
            if any(k.lower() in window for k in _OTP_CONTEXT_KEYWORDS):
                return code
        return all_codes[0]
    return None


# ---- 密码重置链接提取（2FA 补跑顺带设密码用） ----

# 重置链接可信域名 / 路径关键词 / token 参数名（多形态兼容，供实弹校准）
_RESET_HOST = r"(?:chatgpt\.com|auth\.openai\.com)"
_RESET_PATH = r"(?:new-password|reset-password|password-reset|forgot-password)"
_TOKEN_QS = r"(?:[?&](?:token|code|t|reset_token|resetcode|confirmation_token)=[^&\s\"'<>]+)"

# 含 token/code 参数的 reset URL（最高优先级）
_URL_RESET_TOKEN = re.compile(
    r"https?://" + _RESET_HOST + r"/[^\s\"'<>]*" + _RESET_PATH + r"[^\s\"'<>]*" + _TOKEN_QS + r"[^\s\"'<>]*",
    re.IGNORECASE,
)
# 兜底：任意 reset 路径 URL
_URL_RESET_ANY = re.compile(
    r"https?://" + _RESET_HOST + r"/[^\s\"'<>]*" + _RESET_PATH + r"[^\s\"'<>]*",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

_RESET_PARAM_KEYS = ("token", "code", "t", "reset_token", "resetcode", "confirmation_token")


def _reset_url_rank(url: str) -> int:
    """重置链接可信度打分：路径词 > 是否带 token 参数 > 域名。"""
    lower = (url or "").lower()
    rank = 0
    if "/new-password" in lower:
        rank += 4
    elif "/reset-password" in lower or "/password-reset" in lower:
        rank += 3
    elif "/forgot-password" in lower:
        rank += 2
    elif "/reset" in lower:
        rank += 1
    if any(f"{k}=" in lower for k in _RESET_PARAM_KEYS):
        rank += 2
    if lower.startswith("https://chatgpt.com"):
        rank += 1
    return rank


def _mask_token(url: str) -> str:
    """日志脱敏：把 token/code 等参数值掩成前 4 位 + …，避免完整 token 落日志。

    只改参数值，保留 query 原始编码（如 %40 不反转义），非 http(s) 输入返回占位符。
    """
    from urllib.parse import urlsplit, urlunsplit
    if not str(url or "").lower().startswith(("http://", "https://")):
        return "<url>"
    try:
        parts = urlsplit(url)
        masked_q = []
        for seg in (parts.query or "").split("&"):
            if not seg:
                continue
            if "=" in seg:
                k, _, v = seg.partition("=")
                if k in _RESET_PARAM_KEYS and v:
                    v = v[:4] + "…"
                masked_q.append(f"{k}={v}")
            else:
                masked_q.append(seg)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(masked_q), ""))
    except Exception:
        return "<url>"


def extract_reset_link(item: dict) -> str | None:
    """
    从邮件 dict 中抽出密码重置链接（完整 URL，含 token 参数）。

    采集顺序（覆盖字段名容错）：
        1. HTML 字段（content / html / body / body.content / bodyHtml）——含 <a href>
        2. 纯文本字段（text / bodyPreview / bodyText）——覆盖 _html_to_plain_text
           剥掉 <a> 后仍可见的完整 URL 文本
        3. subject（个别邮件把链接放在主题里）

    先抽 <a href> 属性，再跑纯文本 URL 正则（含/不含 token 参数两档），
    全部 html.unescape + 去尾随标点后按 _reset_url_rank 取最优。
    返回 None 表示该邮件不是重置邮件（普通 OTP 邮件等）。
    """
    html_field = _get_field(item, "content", "html", "body", "body.content", "bodyHtml")
    text_field = _get_field(item, "text", "bodyPreview", "bodyText")
    subject = _get_field(item, "subject")
    raw = [s for s in (html_field, text_field, subject) if isinstance(s, str) and s]
    if not raw:
        return None

    href_hits: list[str] = []
    url_hits: list[str] = []
    for s in raw:
        href_hits.extend(html_lib.unescape(h) for h in _HREF_RE.findall(s))
        url_hits.extend(_URL_RESET_TOKEN.findall(s))
        url_hits.extend(_URL_RESET_ANY.findall(s))

    ranked: dict[str, int] = {}
    for u in href_hits + url_hits:
        u = html_lib.unescape(u).strip().rstrip(".,;)]}'\"").strip()
        if not u.lower().startswith(("http://", "https://")):
            continue
        ranked.setdefault(u, _reset_url_rank(u))
    if not ranked:
        return None

    best = max(ranked, key=lambda u: (ranked[u], len(u)))
    logger.info("[ResetLink] 提取到重置链接: %s", _mask_token(best))
    return best
