# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的文件持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import re
import threading
import time
import uuid
from urllib.parse import unquote, urlparse

from flask import Flask, Response, jsonify, make_response, render_template, request

from core import (
    account_token_import,
    codex_retry_service,
    db,
    plan_check_service,
    extract_link_service,
    paypal_payment_service,
    cdk_pool,
    cdk_web_backend,
    codex_agent_service,
    live_check_service,
    recent_mail_service,
    subscription_service,
    twofa_service,
)
from webui.auth import init_auth, register_auth_routes
from core import registration_service as svc
from webui import config_editor

logger = logging.getLogger(__name__)

_TOKENIZED_GENERIC_API_LINE_RE = re.compile(
    r"^(?P<email>.+?)---(?P<discard>.+?)---\s*(?P<code_url>https?://.+)$",
    re.IGNORECASE,
)


def _parse_tokenized_generic_api_line(line: str) -> dict | None:
    """Convert email---token---URL to the generic API pool shape."""
    match = _TOKENIZED_GENERIC_API_LINE_RE.fullmatch(str(line or "").strip())
    if not match:
        return None
    email = match.group("email").strip()
    code_url = match.group("code_url").strip()
    if "@" not in email:
        return None
    return {"email": email, "code_url": code_url}

def _pool_source_arg(default: str = "outlook") -> str:
    src = (request.args.get("source") or "").strip()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = (data.get("source") or data.get("type") or "").strip()
    src = src.lower()
    src = {
        "gmx": "mailcom",
        "gmx.com": "mailcom",
        "caramail": "mailcom",
        "caramail.com": "mailcom",
    }.get(src, src)
    return src if src in ("all", "outlook", "generic_api", "cloudflare_domain", "imap_pass", "mailcom") else default


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "has_refresh_token": bool(row.get("has_refresh_token")),
        "has_pickup_url": bool(row.get("has_pickup_url")),
        "totp_enabled": bool(str(row.get("totp_secret") or "").strip()),
        "has_chatgpt_password": bool(str(row.get("chatgpt_password") or "").strip()),
        "has_registration_proxy": bool(_registration_proxy_copy_value(row)),
    }

    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "note", "archived", "created_at", "group_name",
        "registration_ip", "registration_ip_count",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "oaics_status", "oaics_ok", "oaics_checked_at", "oaics_error",
        "oaics_session_kind", "oaics_method_status", "oaics_method_available",
        "plan_check_status", "codex_status",
        # PayPal 协议提链状态（不包含 AT、密码或代理认证）。
        "extract_link_status", "extract_link_ok", "extract_link_trigger", "extract_link_type",
        "extract_link_queued_at", "extract_link_started_at", "extract_link_completed_at",
        "extract_link_checked_at", "extract_link_job_id", "extract_link_proxy_source",
        "extract_link_backend",
        "paypal_payment_status", "paypal_payment_ok", "paypal_payment_trigger",
        "paypal_payment_country", "paypal_payment_proxy_source", "paypal_payment_queued_at",
        "paypal_payment_started_at", "paypal_payment_completed_at", "paypal_payment_checked_at",
        "paypal_payment_attempt", "paypal_payment_max_attempts", "paypal_payment_settlement_status",
        "paypal_payment_action",
        "paypal_payment_backend",
        # 只保留独立的取消套餐任务状态；账号列表不再展示订阅查询结果。
        "subscription_cancel_status", "subscription_cancel_error",
        "subscription_cancel_queued_at", "subscription_cancel_started_at",
        "subscription_cancel_completed_at", "subscription_cancel_protocol",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_expires_at", "plan_renews_at", "renews_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "token_expired", "token_expires_at", "plan_check_needs_live_check",
        "access_token_invalid", "access_token_status",
        "access_token_status_reason", "access_token_status_checked_at",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        # 2FA 补跑状态。
        "twofa_status", "twofa_error", "twofa_done_at",
        # 补跑顺带设置的密码状态。
        "password_status", "password_error", "password_done_at",
        # Plus 邮件检测状态。
        "plus_mail_status", "plus_mail_hit_subject", "plus_mail_checked_at",
        # Codex 状态提示。
        "codex_error",
        # 结果链接和提链错误只在独立 Paypal协议接口中按需返回。
        "extract_link_payment_method", "extract_link_payment_link_type",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out


def _compact_accounts_for_list(rows: list[dict]) -> list[dict]:
    """Attach non-secret asset flags once, then serialize compact account rows."""
    presence = db.account_asset_presence(rows)
    compact = []
    for raw in rows:
        row = dict(raw)
        row.update(presence.get(int(row.get("id") or 0), {}))
        compact.append(_compact_account_for_list(row))
    return compact


def _xbovo_ship_url(row: dict) -> str:
    """发货导出：把账号邮箱转成 xbovo 取码 URL。

    取账号的邮箱素材（邮箱----key）：优先 original_email_line，否则从邮箱池反查。
    仅当 key 是 alias_ 开头的 xbovo key 时才生成 URL；其它类型（Outlook/mail-api 等）
    返回空串（前端会跳过），避免把 https:// 取码地址再包一层。
    生成格式：https://icloud.xbovo.online/api/v1/code?email=<邮箱>&key=<key>
    """
    email = (row.get("email") or "").strip()
    if not email:
        return ""
    key = ""
    material = str(row.get("original_email_line") or "").strip()
    if material and "----" in material:
        parts = material.split("----")
        if parts and parts[0].strip().lower() == email.lower():
            key = (parts[1] if len(parts) > 1 else "").strip()
    if not key:
        # 从通用 API 邮箱池按邮箱反查 xbovo key
        try:
            from core import db
            pool_row = db.get_generic_api_email_by_email(email)
            key = (pool_row or {}).get("code_url") or ""
        except Exception:
            key = ""
    key = (key or "").strip()
    if not key.startswith("alias_"):
        return ""
    from urllib.parse import quote
    url = f"https://icloud.xbovo.online/api/v1/code?email={quote(email)}&key={quote(key)}"
    # 发货格式：邮箱---URL（三条横线分隔），一行一条
    return f"{email}---{url}"


def _ship_line(row: dict) -> str:
    """发货导出格式：邮箱----密码----2FA密钥----AT，一行一条。

    无 ChatGPT 登录密码时返回空串（前端 filter(Boolean) 会跳过未设密码账号），
    避免导出空密码字段的残缺行。
    """
    email = (row.get("email") or "").strip()
    password = (row.get("chatgpt_password") or "").strip()
    if not email or not password:
        return ""
    return "----".join([
        email,
        password,
        (row.get("totp_secret") or "").strip(),
        (row.get("access_token") or "").strip(),
    ])


def _delivery_line(row: dict) -> str:
    """Delivery format: email----password----2FA secret."""
    parts = [
        str(row.get("email") or "").strip(),
        str(row.get("chatgpt_password") or "").strip(),
        str(row.get("totp_secret") or "").strip(),
    ]
    return "----".join(parts) if all(parts) else ""


def _free_line(row: dict) -> str:
    """FREE format: email----password----2FA secret----AT."""
    parts = [
        str(row.get("email") or "").strip(),
        str(row.get("chatgpt_password") or "").strip(),
        str(row.get("totp_secret") or "").strip(),
        str(row.get("access_token") or "").strip(),
    ]
    return "----".join(parts) if all(parts) else ""


def _material_line_for_account(row: dict) -> str:
    """账号的完整邮箱信息（导入时的那一行素材，如 邮箱----取件url）。

    优先用 original_email_line（注册时落库的素材行）；缺失时从对应邮箱池反查：
      - 通用 API / xbovo 池 → 邮箱----code_url（key）
      - Outlook 池 → 邮箱----密码----clientId----refreshToken
    都没有时退回邮箱地址本身。
    """
    email = (row.get("email") or "").strip()
    if not email:
        return ""

    original = str(row.get("original_email_line") or "").strip()
    if original:
        return original

    try:
        from core import db
        # 先按邮箱查通用 API / xbovo 池
        pool_row = db.get_generic_api_email_by_email(email)
        if pool_row and pool_row.get("code_url"):
            return f"{email}----{pool_row.get('code_url')}"
        # mail.com / GMX / Caramail 共用邮箱池
        mailcom_row = db.get_mailcom_email_by_email(email)
        if mailcom_row:
            return f"{email}----{mailcom_row.get('password') or ''}"
        # 再查 Outlook 池
        outlook_row = db.get_outlook_by_email(email)
        if outlook_row:
            parts = [
                email,
                outlook_row.get("password") or "",
                outlook_row.get("client_id") or "",
                outlook_row.get("refresh_token") or "",
            ]
            return "----".join(parts)
    except Exception:
        pass

    return email


def _account_code_url(email: str) -> str:
    """取件地址：账号注册用的取件 URL / xbovo key。从通用 API 池反查。"""
    email = (email or "").strip()
    if not email:
        return ""
    try:
        from core import db
        pool_row = db.get_generic_api_email_by_email(email)
        return (pool_row or {}).get("code_url") or ""
    except Exception:
        return ""


def _account_pickup_url(row: dict) -> str:
    """Return the usable pickup URL for one account without exposing it in list APIs."""
    email = str(row.get("email") or "").strip()
    if not email:
        return ""
    value = _account_code_url(email)
    if not value:
        original = str(row.get("original_email_line") or "").strip()
        if "----" in original:
            parts = original.split("----", 2)
            value = str(parts[1] if len(parts) > 1 else "").strip()
    value = str(value or "").strip()
    if value.startswith("alias_"):
        from urllib.parse import quote
        return f"https://icloud.xbovo.online/api/v1/code?email={quote(email)}&key={quote(value)}"
    return value if value.startswith(("http://", "https://")) else ""


def _registration_proxy_copy_value(row: dict) -> str:
    """注册代理复制格式：host:port:username:password；列表接口只返回 presence。"""
    raw = str(row.get("registration_proxy") or row.get("proxy_used") or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        parts = raw.split(":", 3)
        if len(parts) == 4 and parts[0] and parts[1].isdigit():
            return raw
        return ""
    try:
        parsed = urlparse(raw)
        host = str(parsed.hostname or "").strip()
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if not host or not port:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    return f"{host}:{port}:{username}:{password}"


def _account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "codex_refresh_token":
        return db.get_codex_refresh_token(
            str(row.get("email") or ""),
            account_id=str(row.get("account_id") or ""),
        )
    if field == "totp_secret":
        return str(row.get("totp_secret") or "").strip()
    if field == "copy_line":
        return str(row.get("copy_line") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    if field == "material_line":
        # 邮箱信息（导入时的那一行完整素材，如 邮箱----取件url，不含 token）
        return _material_line_for_account(row)
    if field == "material_with_at":
        # 邮箱----取件url ---- at（邮箱素材 + access token，---- 与 at 之间留空格，一行一个）
        material = _material_line_for_account(row)
        token = str(row.get("access_token") or "").strip()
        if material and token:
            return f"{material} ---- {token}"
        return material or ""
    if field == "xbovo_ship_url":
        # 发货导出：把 xbovo 账号的 邮箱----alias_key 转成取码 URL，一行一个
        return _xbovo_ship_url(row)
    if field == "chatgpt_password":
        return str(row.get("chatgpt_password") or "")
    if field == "pickup_url":
        return _account_pickup_url(row)
    if field == "registration_proxy":
        return _registration_proxy_copy_value(row)
    if field == "delivery_line":
        return _delivery_line(row)
    if field == "free_line":
        return _free_line(row)
    if field == "ship_line":
        # 发货导出：邮箱----密码----2FA密钥----AT，一行一个
        return _ship_line(row)
    raise ValueError("field 仅支持 access_token/codex_refresh_token/totp_secret/copy_line/codex_agent_token/material_line/material_with_at/xbovo_ship_url/chatgpt_password/pickup_url/registration_proxy/delivery_line/free_line/ship_line")


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required", "registration_country",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    return out


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts

def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    _prepared_downloads: dict[str, dict] = {}

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理 10 分钟前的临时下载，避免内存堆积。
        for k, v in list(_prepared_downloads.items()):
            if now - float(v.get("created_at") or 0) > 600:
                _prepared_downloads.pop(k, None)
        download_id = uuid.uuid4().hex
        _prepared_downloads[download_id] = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.pop(str(download_id or ""), None)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)
    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_subscription_cancels = db.recover_interrupted_subscription_cancels()
    if recovered_subscription_cancels:
        logger.warning(
            "已恢复 %s 个因 WebUI 重启中断的订阅取消状态",
            recovered_subscription_cancels,
        )
    recovered_extract_links = db.recover_interrupted_extract_links()
    if recovered_extract_links:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
    recovered_paypal_payments = db.recover_interrupted_paypal_payments()
    if recovered_paypal_payments:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的协议支付状态", recovered_paypal_payments)
    recovered_live_checks = db.recover_interrupted_live_checks()
    if recovered_live_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
    recovered_twofa = db.recover_interrupted_twofa()
    if recovered_twofa:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 2FA 补跑状态", recovered_twofa)
    recovered_codex_agents = db.recover_interrupted_codex_agents()
    if recovered_codex_agents:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)
    try:
        recovered_cdk_leases = cdk_pool.get_pool().recover_orphans()
        if recovered_cdk_leases:
            logger.warning("已回收 %s 个因 WebUI 重启遗留的 CDK 租约", recovered_cdk_leases)
    except Exception:
        logger.exception("恢复 CDK 租约失败")

    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        requested_ui = (request.args.get("ui") or "").strip().lower()
        if requested_ui in {"legacy", "modern"}:
            ui_mode = requested_ui
        else:
            ui_mode = (request.cookies.get("ui_mode") or "modern").strip().lower()
            if ui_mode not in {"legacy", "modern"}:
                ui_mode = "modern"

        template_name = "index_legacy.html" if ui_mode == "legacy" else "index.html"
        resp = make_response(render_template(template_name))
        if requested_ui in {"legacy", "modern"}:
            resp.set_cookie("ui_mode", ui_mode, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        for src in parse_email_sources(_email_cfg.EMAIL_SOURCE):
            # GPTMail/MailNest/CloudMail 地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare"):
                continue
            one = (
                db.generic_api_email_pool_summary() if src == "generic_api"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else db.imap_email_pool_summary() if src == "imap_pass"
                else db.mailcom_email_pool_summary() if src == "mailcom"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        registration_ip = str(request.args.get("registration_ip", default="") or "").strip()
        account_group = str(request.args.get("group", default="") or "").strip()
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(
                limit=page_size,
                offset=offset,
                archived=archived,
                plan_filter=plan_filter,
                q=q,
                registration_ip=registration_ip,
                account_group=account_group,
            )
            result["items"] = _compact_accounts_for_list(result.get("items") or [])
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        rows = db.list_accounts(
            limit=limit,
            archived=archived,
            plan_filter=plan_filter,
            q=q,
            registration_ip=registration_ip,
            account_group=account_group,
        )
        # 兼容旧调用仍返回数组，但内容与分页接口保持同一份精简结构。
        # 代理账号密码等敏感字段只允许通过 /secret 按需读取。
        return jsonify(_compact_accounts_for_list(rows))

    @app.post("/api/accounts/import-tokens")
    def api_accounts_import_tokens():
        """Import accounts from AT, RT, AT----RT, or Codex token JSON."""
        data = request.get_json(silent=True) or {}
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return jsonify({"ok": False, "error": "请粘贴 AT、RT 或 Token JSON"}), 400
        try:
            result = account_token_import.import_account_tokens(text)
        except account_token_import.TokenImportError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.warning("Token 导入接口异常 type=%s", type(exc).__name__)
            return jsonify({"ok": False, "error": "Token 导入处理失败，请查看服务日志"}), 500
        return jsonify(result)

    @app.get("/api/accounts/groups")
    def api_account_groups():
        """Return account-group names/counts for filters and batch assignment."""
        include_archived = str(request.args.get("include_archived", default="1") or "1").lower() in {"1", "true", "yes"}
        return jsonify({"ok": True, **db.list_account_groups(include_archived=include_archived)})

    @app.post("/api/accounts/group-bulk")
    def api_accounts_group_bulk():
        """Assign selected accounts to one group. Empty group_name removes grouping."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        group_name = str(data.get("group_name") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多分组 5000 个账号"}), 400
        if len(group_name) > 64:
            return jsonify({"ok": False, "error": "分组名称最多 64 个字符"}), 400
        if group_name.casefold() == "__ungrouped__":
            return jsonify({"ok": False, "error": "该名称为系统保留的未分组筛选值"}), 400
        updated, skipped = db.update_accounts_group(account_ids=ids, group_name=group_name)
        return jsonify({
            "ok": True,
            "group_name": group_name,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/accounts/plus-check")
    def api_accounts_plus_check():
        """
        批量检测选中账号的取件邮箱里是否有含 "plus" 单词的邮件（判断是否开通 Plus）。
        Body {account_ids:[...]}。对每个账号取其取件 URL 拉邮件列表，检查 subject/正文是否含 plus。
        """
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 200:
            return jsonify({"ok": False, "error": "单次最多检测 200 个账号"}), 400

        from core.generic_api_mail_client import fetch_mail_items_for_url

        results = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            code_url = _account_code_url(email)
            if not code_url:
                skipped.append({"id": acc_id, "email": email, "reason": "没有取件地址"})
                continue
            try:
                mails = fetch_mail_items_for_url(code_url, email=email)
                has_plus = False
                hit_subject = ""
                for it in mails:
                    blob = f"{it.get('subject', '')}\n{it.get('text', '')}".lower()
                    if "plus" in blob:
                        has_plus = True
                        hit_subject = str(it.get("subject") or "") or hit_subject
                        break
                status = "plus" if has_plus else "no_plus"
                # 只回传最新几封 + 截断正文，避免几十账号×百封邮件的 JSON 过大把前端卡死
                view_mails = []
                for it in (mails or [])[:5]:
                    text = str(it.get("text") or "")
                    view_mails.append({
                        "subject": str(it.get("subject") or "")[:300],
                        "text": text[:2000],
                        "received_at": it.get("received_at"),
                        "from": it.get("from"),
                    })
                results.append({
                    "id": acc_id,
                    "email": email,
                    "has_plus": has_plus,
                    "hit_subject": hit_subject,
                    "checked": len(mails),
                    "status": status,
                    "mails": view_mails,
                })
                # 写回账号：账号列表展示 Plus 邮件状态
                try:
                    db.update_account_plus_mail(acc_id=acc_id, status=status, hit_subject=hit_subject)
                except Exception:
                    pass
            except Exception as exc:
                skipped.append({"id": acc_id, "email": email, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return jsonify({
            "ok": True,
            "results": results,
            "count": len(results),
            "plus_count": sum(1 for r in results if r.get("has_plus")),
            "skipped": skipped,
        })

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        raw_ids = request.args.get("ids")
        if raw_ids is not None:
            pieces = [part.strip() for part in str(raw_ids).split(",") if part.strip()]
            if not pieces or len(pieces) > 500:
                return jsonify({"ok": False, "error": "ids 必须包含 1 到 500 个账号 ID"}), 400
            try:
                account_ids = list(dict.fromkeys(int(part) for part in pieces))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "ids 包含非法账号 ID"}), 400
            if any(item <= 0 for item in account_ids):
                return jsonify({"ok": False, "error": "ids 包含非法账号 ID"}), 400
            snapshot = db.list_account_plan_check_statuses(
                limit=len(account_ids),
                account_ids=account_ids,
                archived="all",
            )
            returned_ids = {int(item.get("id") or 0) for item in snapshot.get("items") or []}
            snapshot.update({
                "requested_count": len(account_ids),
                "missing_ids": [item for item in account_ids if item not in returned_ids],
                "queue": plan_check_service.queue_settings(),
            })
            return jsonify(snapshot)

        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        q = str(request.args.get("q", default="") or "").strip()
        registration_ip = str(request.args.get("registration_ip", default="") or "").strip()
        account_group = str(request.args.get("group", default="") or "").strip()
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(
                limit=page_size,
                offset=offset,
                archived=archived,
                plan_filter=plan_filter,
                q=q,
                registration_ip=registration_ip,
                account_group=account_group,
            )
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(
                limit=max(1, min(5000, limit)),
                archived=archived,
                plan_filter=plan_filter,
                q=q,
                registration_ip=registration_ip,
                account_group=account_group,
            )
        snapshot["queue"] = plan_check_service.queue_settings()
        return jsonify(snapshot)


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = _account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        field = str(data.get("field") or "").strip()
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                value = _account_secret_value(acc, field)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/delete-free-without-trial")
    def api_accounts_delete_free_without_trial():
        """预览或删除已确认无 Plus 试用资格的普通 free 账号。"""
        data = request.get_json(silent=True) or {}
        dry_run = data.get("dry_run", True) is not False
        if not dry_run and data.get("confirm") is not True:
            return jsonify({"ok": False, "error": "删除前必须显式确认"}), 400
        cleanup_kwargs = {"dry_run": dry_run, "include_archived": True}
        raw_ids = data.get("candidate_ids")
        if not dry_run:
            if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 5000:
                return jsonify({"ok": False, "error": "candidate_ids 必须是 1 到 5000 个预览候选 ID"}), 400
        elif raw_ids is not None and (not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 5000):
            return jsonify({"ok": False, "error": "candidate_ids 必须是 1 到 5000 个选中账号 ID"}), 400
        if raw_ids is not None:
            try:
                candidate_ids = list(dict.fromkeys(int(item) for item in raw_ids))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "candidate_ids 包含非法 ID"}), 400
            if any(item <= 0 for item in candidate_ids):
                return jsonify({"ok": False, "error": "candidate_ids 包含非法 ID"}), 400
            cleanup_kwargs["candidate_ids"] = candidate_ids
        result = db.cleanup_free_accounts_without_plus_trial(**cleanup_kwargs)
        return jsonify({"ok": True, **result})

    @app.post("/api/accounts/delete-invalid-at")
    @app.post("/api/accounts/delete-invalid-token")
    def api_accounts_delete_invalid_at():
        """预览或删除已明确判定 AT 失效的账号。"""
        data = request.get_json(silent=True) or {}
        dry_run = data.get("dry_run", True) is not False
        if not dry_run and data.get("confirm") is not True:
            return jsonify({"ok": False, "error": "删除前必须显式确认"}), 400

        cleanup_kwargs: dict = {"dry_run": dry_run}
        raw_ids = data.get("candidate_ids")
        if not dry_run:
            if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 5000:
                return jsonify({"ok": False, "error": "candidate_ids 必须是 1 到 5000 个预览候选 ID"}), 400
        elif raw_ids is not None and (not isinstance(raw_ids, list) or len(raw_ids) > 5000):
            return jsonify({"ok": False, "error": "candidate_ids 必须是数组且最多 5000 个"}), 400

        if raw_ids is not None:
            try:
                candidate_ids = list(dict.fromkeys(int(item) for item in raw_ids))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "candidate_ids 包含非法 ID"}), 400
            if any(item <= 0 for item in candidate_ids):
                return jsonify({"ok": False, "error": "candidate_ids 包含非法 ID"}), 400
            cleanup_kwargs["candidate_ids"] = candidate_ids

        result = db.cleanup_accounts_with_invalid_at(**cleanup_kwargs)
        return jsonify({"ok": True, **result})

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：使用账号密码和 2FA 在 Roxy 浏览器中登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # Roxy profile 自己管理浏览器网络环境；不复用套餐协议代理选路。
                proxy=None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/setup-2fa-bulk")
    def api_accounts_setup_2fa_bulk():
        """批量补跑 2FA：后台队列里重新 OTP 登录 + reauth 设置 TOTP，写回 secret。

        Body {account_ids:[...], proxy?}。会为每个账号消耗 2 封邮箱 OTP
        （重新登录 1 封 + reauth 重认证 1 封）。
        """
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多补跑 500 个账号"}), 400
        proxy = data.get("proxy") if "proxy" in data else None

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("email") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "邮箱为空"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = twofa_service.enqueue_account_twofa(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                trigger="manual_bulk",
                proxy=proxy,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202


    @app.post("/api/accounts/subscriptions/cancel-batch")
    def api_accounts_cancel_subscriptions():
        """Queue automatic-renewal cancellation for selected accounts only."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 200:
            return jsonify({"ok": False, "error": "单次最多取消 200 个账号的套餐"}), 400

        queue = subscription_service.queue_settings()
        default_concurrency = int(queue.get("default_concurrency") or queue.get("workers") or 2)
        max_concurrency = int(queue.get("max_concurrency") or 20)
        concurrency = data.get("concurrency", default_concurrency)
        if type(concurrency) is not int or not 1 <= concurrency <= max_concurrency:
            return jsonify({
                "ok": False,
                "error": f"concurrency 必须是 1 到 {max_concurrency} 之间的整数",
            }), 400
        cancel_batch = subscription_service.create_subscription_cancel_batch(concurrency)

        results: list[dict] = []
        seen: set[int] = set()
        for raw in ids:
            if type(raw) is not int or raw <= 0:
                results.append({"id": raw, "status": "failed", "ok": False, "error": "ID 非法"})
                continue
            acc_id = raw
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                results.append({"id": acc_id, "status": "failed", "ok": False, "error": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                results.append({"id": acc_id, "status": "failed", "ok": False, "error": "邮箱为空"})
                continue

            queued = subscription_service.enqueue_account_subscription_cancel(
                account_id=acc_id,
                email=email,
                trigger="manual_bulk",
                batch=cancel_batch,
            )
            if queued.get("accepted"):
                results.append({
                    "id": acc_id,
                    "email": email,
                    "status": "queued",
                    "ok": None,
                    "loading": True,
                    "cancelling": True,
                    "message": "已加入取消套餐队列",
                })
            elif queued.get("busy"):
                results.append({
                    "id": acc_id,
                    "email": email,
                    "status": str(acc.get("subscription_cancel_status") or "running"),
                    "ok": None,
                    "loading": True,
                    "cancelling": True,
                    "message": queued.get("error") or "该账号正在处理取消套餐",
                })
            else:
                results.append({
                    "id": acc_id,
                    "email": email,
                    "status": "failed",
                    "ok": False,
                    "loading": False,
                    "cancelling": False,
                    "error": queued.get("error") or "取消套餐入队失败",
                })

        cancel_batch.seal()
        queued_count = sum(1 for item in results if item.get("status") == "queued")
        failed_count = sum(1 for item in results if item.get("status") == "failed")
        return jsonify({
            "ok": True,
            "results": results,
            "queued_count": queued_count,
            "failed_count": failed_count,
            "concurrency": concurrency,
            "queue": queue,
        }), 202

    @app.get("/api/accounts/subscriptions/cancel-status")
    def api_accounts_cancel_subscription_status():
        """Return compact progress for a selected cancellation batch."""
        raw_ids = str(request.args.get("ids") or "").strip()
        if not raw_ids:
            return jsonify({"ok": False, "error": "ids 不能为空"}), 400
        pieces = [part.strip() for part in raw_ids.split(",") if part.strip()]
        if not pieces or len(pieces) > 200:
            return jsonify({"ok": False, "error": "ids 必须包含 1 到 200 个账号 ID"}), 400
        try:
            account_ids = list(dict.fromkeys(int(part) for part in pieces))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "ids 包含非法账号 ID"}), 400
        if any(item <= 0 for item in account_ids):
            return jsonify({"ok": False, "error": "ids 包含非法账号 ID"}), 400

        results = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                results.append({"id": acc_id, "status": "failed", "ok": False, "error": "账号不存在"})
                continue
            cancel_status = str(acc.get("subscription_cancel_status") or "unknown")
            terminal_ok = cancel_status == "success"
            terminal_failed = cancel_status == "failed"
            cancelling = cancel_status in {"queued", "running"}
            results.append({
                "id": acc_id,
                "email": acc.get("email"),
                "status": cancel_status,
                "loading": cancelling,
                "cancelling": cancelling,
                "ok": True if terminal_ok else False if terminal_failed else None,
                "error": acc.get("subscription_cancel_error") if terminal_failed else None,
                "reason": acc.get("subscription_cancel_outcome"),
                "message": acc.get("subscription_cancel_message"),
                "subscription_status": acc.get("subscription_status"),
                "cancels_at": acc.get("plan_cancels_at"),
                "protocol": acc.get("subscription_cancel_protocol"),
                "completed_at": acc.get("subscription_cancel_completed_at"),
            })
        return jsonify({"ok": True, "results": results})

    @app.get("/api/accounts/<int:acc_id>/subscription-cancel-log")
    def api_account_subscription_cancel_log(acc_id: int):
        """Return one account's redacted cancellation log and lightweight state."""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404

        email = str(acc.get("email") or "").strip()
        status = str(acc.get("subscription_cancel_status") or "").strip().lower()
        running = subscription_service.is_cancelling(email) or status in {"queued", "running"}
        return jsonify({
            "ok": True,
            "id": int(acc_id),
            "email": email,
            "status": status or "idle",
            "running": running,
            "log": subscription_service.read_cancel_log(email),
        })


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        requested_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            requested_ids.append(acc_id)

        accounts_by_id = {
            int(acc.get("id") or 0): acc
            for acc in db.get_accounts_by_ids(requested_ids)
        }
        items = []
        for acc_id in requested_ids:
            acc = accounts_by_id.get(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.get("/api/extract-link/cdk")
    def api_extract_link_cdk():
        """查询当前配置或传入 CDK 的剩余次数。"""
        code = (request.args.get("code") or "").strip() or None
        try:
            return jsonify({"ok": True, **extract_link_service.query_cdk(cdk=code)})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    def _is_extract_eligible(acc: dict) -> bool:
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").lower()
        return plan == "free" and bool(acc.get("plus_trial_eligible"))

    @app.get("/api/paypal-protocol/cdk")
    @app.get("/api/paypal-protocol/cdk/status")
    def api_paypal_protocol_cdk_pool():
        """CDK 池只返回脱敏值、剩余次数和租约状态。"""
        pool = cdk_pool.get_pool()
        items = pool.list_public()
        counts: dict[str, int] = {}
        for item in items:
            value = str(item.get("status") or "available")
            counts[value] = counts.get(value, 0) + 1
        return jsonify({
            "ok": True,
            "items": items,
            "total": len(items),
            "available": counts.get("available", 0),
            "counts": counts,
            "settings": cdk_web_backend.public_settings(),
            "queue": cdk_web_backend.queue_settings(),
        })

    @app.post("/api/paypal-protocol/cdk/import")
    def api_paypal_protocol_cdk_import():
        """导入多行 CDK。Body {codes: "一行一个"|[...], replace?}。"""
        data = request.get_json(silent=True) or {}
        values = data.get("codes", data.get("text", data.get("items", "")))
        if not isinstance(values, (str, list, tuple)):
            return jsonify({"ok": False, "error": "codes 必须是多行字符串或数组"}), 400
        lines = values.splitlines() if isinstance(values, str) else list(values)
        if len(lines) > 10000:
            return jsonify({"ok": False, "error": "单次最多导入 10000 条 CDK"}), 400
        if not any(str(item or "").strip() for item in lines):
            return jsonify({"ok": False, "error": "没有可导入的 CDK"}), 400
        replace = bool(data.get("replace")) or str(data.get("mode") or "").lower() == "replace"
        result = cdk_pool.get_pool().import_codes(lines, replace=replace)
        return jsonify({"ok": True, **result})

    @app.post("/api/paypal-protocol/cdk/delete")
    def api_paypal_protocol_cdk_delete():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or data.get("cdk_ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "ids 必须是非空数组"}), 400
        if len(ids) > 10000:
            return jsonify({"ok": False, "error": "单次最多删除 10000 条 CDK"}), 400
        return jsonify({"ok": True, **cdk_pool.get_pool().delete(ids)})

    @app.post("/api/paypal-protocol/cdk/reset")
    def api_paypal_protocol_cdk_reset():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids", data.get("cdk_ids"))
        if ids is not None and not isinstance(ids, list):
            return jsonify({"ok": False, "error": "ids 必须是数组"}), 400
        return jsonify({"ok": True, **cdk_pool.get_pool().reset(ids)})

    def _enqueue_cdk_account(acc: dict, *, trigger: str, data: dict) -> dict:
        if not _is_extract_eligible(acc):
            return {"accepted": False, "busy": False, "error": "仅支持 free(可Plus试用) 账号提链"}
        token = str(acc.get("access_token") or "").strip()
        if not token:
            return {"accepted": False, "busy": False, "error": "该账号没有 access_token"}
        return cdk_web_backend.enqueue_extract(
            account_id=int(acc.get("id")),
            email=str(acc.get("email") or ""),
            access_token=token,
            trigger=trigger,
            proxy=data.get("proxy") if "proxy" in data else None,
        )

    @app.post("/api/paypal-protocol/cdk/extract")
    @app.post("/api/paypal-protocol/cdk/retry")
    def api_paypal_protocol_cdk_extract():
        data = request.get_json(silent=True) or {}
        try:
            acc = db.get_account(int(data.get("account_id") or data.get("id")))
        except (TypeError, ValueError):
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            queued = _enqueue_cdk_account(acc, trigger="cdk_manual", data=data)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **{k: v for k, v in queued.items() if k != "future"}}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **{k: v for k, v in queued.items() if k != "future"}}), 400
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/paypal-protocol/cdk/extract-bulk")
    def api_paypal_protocol_cdk_extract_bulk():
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400
        started, busy, failed, skipped, seen = [], [], [], [], set()
        for raw in ids:
            try:
                account_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if account_id in seen:
                continue
            seen.add(account_id)
            acc = db.get_account(account_id)
            if not acc:
                skipped.append({"id": account_id, "reason": "账号不存在"})
                continue
            try:
                queued = _enqueue_cdk_account(acc, trigger="cdk_manual_bulk", data=data)
            except Exception as exc:
                queued = {"accepted": False, "busy": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
            item = {"id": account_id, "email": acc.get("email"), **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started, "started_count": len(started),
            "busy": busy, "busy_count": len(busy),
            "failed": failed, "failed_count": len(failed),
            "skipped": skipped, "skipped_count": len(skipped),
        }), 202

    @app.post("/api/paypal-protocol/cdk/intervention/<kind>")
    @app.post("/api/paypal-protocol/cdk/otp")
    @app.post("/api/paypal-protocol/cdk/captcha")
    def api_paypal_protocol_cdk_intervention(kind: str = ""):
        if not kind:
            kind = request.path.rsplit("/", 1)[-1]
        kind = str(kind or "").lower()
        if kind not in {"otp", "captcha"}:
            return jsonify({"ok": False, "error": "kind 仅支持 otp/captcha"}), 400
        data = request.get_json(silent=True) or {}
        try:
            account_id = int(data.get("account_id") or data.get("id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "account_id 无效"}), 400
        value = str(data.get("value") or data.get("code") or "").strip()
        if not value:
            return jsonify({"ok": False, "error": "value 不能为空"}), 400
        try:
            result = cdk_web_backend.submit_intervention(account_id=account_id, value=value, kind=kind)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}), 400
        # The submit call only acknowledges the remote task input.  The same
        # payment task is polled asynchronously and its final state is written
        # back to the account record by the CDK backend.
        return jsonify({
            "ok": True,
            "accepted": bool(result.get("accepted", True)) if isinstance(result, dict) else True,
            "status": result.get("status", "running") if isinstance(result, dict) else "running",
            "kind": result.get("kind", kind) if isinstance(result, dict) else kind,
            "protocol_job_id": result.get("protocol_job_id") if isinstance(result, dict) else None,
        }), 202

    @app.post("/api/accounts/extract-link")
    @app.post("/api/paypal-protocol/extract")
    def api_account_extract_link():
        """单账号 PayPal 提链。Body {account_id|id, proxy?, payment_proxy?, link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_extract_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free(可Plus试用) 账号提链；请先查询套餐确认资格"}), 400
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            enqueue_kwargs = {
                "account_id": int(acc.get("id")),
                "email": acc.get("email") or "",
                "access_token": token,
                "trigger": "manual",
                "link_type": data.get("link_type"),
                "cdk": data.get("cdk"),
                "proxy": data.get("proxy") if "proxy" in data else None,
            }
            # payment_proxy is a local full-pipeline override.  The CDK
            # workbench owns its entire payment leg, so never include this
            # field in the CDK extraction task context.
            if extract_link_service.backend_name() == "local" and "payment_proxy" in data:
                enqueue_kwargs["payment_proxy"] = data.get("payment_proxy")
            queued = extract_link_service.enqueue_account_extract(
                **enqueue_kwargs,
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/extract-link-bulk")
    @app.post("/api/paypal-protocol/extract-bulk")
    def api_accounts_extract_link_bulk():
        """批量 PayPal 提链。Body {account_ids:[...], proxy?, payment_proxy?, link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        local_route = extract_link_service.backend_name() == "local"
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if not _is_extract_eligible(acc):
                skipped.append({"id": acc_id, "email": email, "reason": "不是 free(可Plus试用)"})
                continue
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                enqueue_kwargs = {
                    "account_id": acc_id,
                    "email": email or "",
                    "access_token": token,
                    "trigger": "manual_bulk",
                    "link_type": data.get("link_type"),
                    "cdk": data.get("cdk"),
                    "proxy": data.get("proxy") if "proxy" in data else None,
                }
                if local_route and "payment_proxy" in data:
                    enqueue_kwargs["payment_proxy"] = data.get("payment_proxy")
                queued = extract_link_service.enqueue_account_extract(**enqueue_kwargs)
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    @app.post("/api/paypal-protocol/payment")
    @app.post("/api/paypal-protocol/cdk/payment")
    @app.post("/api/accounts/paypal-payment")
    def api_account_paypal_payment():
        """为已提链成功账号启动一次 PayPal BA 协议支付。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc_id = int(acc_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "account_id 无效"}), 400
        try:
            queued = paypal_payment_service.enqueue_account_payment(
                account_id=acc_id,
                trigger="manual",
                proxy=data.get("proxy") if "proxy" in data else None,
                country=data.get("country") if "country" in data else None,
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **{k: v for k, v in queued.items() if k != "future"}}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **{k: v for k, v in queued.items() if k != "future"}}), 400
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/paypal-protocol/payment-bulk")
    @app.post("/api/paypal-protocol/cdk/payment-bulk")
    @app.post("/api/accounts/paypal-payment-bulk")
    def api_accounts_paypal_payment_bulk():
        """批量启动协议支付；只处理提链成功且链接未过期的账号。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("record_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多支付 500 个账号"}), 400
        started, busy, failed, skipped = [], [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            try:
                queued = paypal_payment_service.enqueue_account_payment(
                    account_id=acc_id,
                    trigger="manual_bulk",
                    proxy=data.get("proxy") if "proxy" in data else None,
                    country=data.get("country") if "country" in data else None,
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
                continue
            item = {"id": acc_id, "email": acc.get("email"), **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started, "started_count": len(started),
            "busy": busy, "busy_count": len(busy),
            "failed": failed, "failed_count": len(failed),
            "skipped": skipped, "skipped_count": len(skipped),
        }), 202

    @app.post("/api/paypal-protocol/delete-bulk")
    @app.post("/api/paypal-protocol/records/delete-bulk")
    def api_paypal_protocol_delete_bulk():
        """批量删除 Paypal协议页记录，不删除账号本体。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("record_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 条记录"}), 400
        try:
            account_ids = list(dict.fromkeys(int(item) for item in ids))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "account_ids 包含非法 ID"}), 400
        cleared, skipped = db.clear_paypal_protocol_records(account_ids)
        return jsonify({"ok": True, "deleted": cleared, "deleted_count": len(cleared), "skipped": skipped})

    @app.post("/api/paypal-protocol/export-delivery")
    @app.post("/api/paypal-protocol/ship-export")
    def api_paypal_protocol_export_delivery():
        """导出选中的支付成功账号发货行。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("record_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多导出 5000 个账号"}), 400
        lines, skipped, seen = [], [], set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if str(acc.get("paypal_payment_status") or "").lower() != "success":
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "不是支付成功账号"})
                continue
            line = _ship_line(acc)
            if not line:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 ChatGPT 密码，无法生成发货行"})
                continue
            lines.append(line)
        if not lines:
            return jsonify({"ok": False, "error": "没有可导出的支付成功账号", "skipped": skipped}), 400
        from datetime import datetime as _dt
        filename = f"paypal-payment-delivery-{_dt.now().strftime('%Y%m%d-%H%M%S')}.txt"
        body = ("\n".join(lines) + "\n").encode("utf-8")
        return Response(body, mimetype="text/plain; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(len(lines)),
            "X-Skipped-Count": str(len(skipped)),
        })

    @app.post("/api/paypal-protocol/setup-2fa")
    @app.post("/api/paypal-protocol/repair-2fa")
    def api_paypal_protocol_setup_2fa():
        """只对选中的支付成功账号启动补跑 2FA。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("record_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多补跑 500 个账号"}), 400
        started, busy, failed, skipped = [], [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if str(acc.get("paypal_payment_status") or "").lower() != "success":
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "不是支付成功账号"})
                continue
            queued = twofa_service.enqueue_account_twofa(
                account_id=acc_id,
                email=acc.get("email") or "",
                trigger="paypal_payment_success",
                proxy=data.get("proxy") if "proxy" in data else None,
            )
            item = {"id": acc_id, "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started, "started_count": len(started),
            "busy": busy, "busy_count": len(busy),
            "failed": failed, "failed_count": len(failed),
            "skipped": skipped, "skipped_count": len(skipped),
        }), 202

    @app.get("/api/paypal-protocol")
    def api_paypal_protocol():
        """Paypal协议独立页面数据；返回提链/支付状态和脱敏账号属性。"""
        status = str(request.args.get("status", default="all") or "all").strip().lower()
        bucket = str(request.args.get("bucket", default="all") or "all").strip().lower()
        q = str(request.args.get("q", default="") or "").strip()
        limit = request.args.get("limit", default=100, type=int)
        offset = request.args.get("offset", default=0, type=int)
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_size_arg is not None:
            limit = page_size_arg
        if page_arg is not None:
            offset = (max(1, page_arg) - 1) * max(1, limit or 100)
        limit = int(limit or 100)
        offset = int(offset or 0)
        if status not in {"all", "queued", "running", "success", "failed", "expired", "stopped", "payment_success", "extract_only", "not_extracted", "payment_queued", "payment_running", "payment_failed"}:
            return jsonify({"ok": False, "error": "status 参数无效"}), 400
        if bucket not in {"all", "payment_success", "extract_only", "not_extracted"}:
            return jsonify({"ok": False, "error": "bucket 参数无效"}), 400
        snapshot = db.list_paypal_protocol_links(
            limit=max(1, min(500, limit)),
            offset=max(0, offset),
            status=status,
            bucket=bucket,
            q=q,
        )
        snapshot.update({
            "ok": True,
            "page": max(1, int(offset // max(1, snapshot.get("limit") or 1) + 1)),
            "page_size": snapshot.get("limit"),
            "mode": extract_link_service.mode_state(),
            "settings": {
                **extract_link_service.public_settings(),
                **paypal_payment_service.public_settings(),
                **cdk_web_backend.public_settings(),
            },
            "payment_settings": paypal_payment_service.public_settings(),
            "queue": {"extract": extract_link_service.queue_settings(), "payment": paypal_payment_service.queue_settings(), "cdk_web": cdk_web_backend.queue_settings()},
        })
        return jsonify(snapshot)

    @app.get("/api/paypal-protocol/settings")
    def api_paypal_protocol_settings():
        """读取 Paypal协议页设置；代理只返回是否已配置。"""
        return jsonify({
            "ok": True,
            "mode": extract_link_service.mode_state(),
            "settings": {
                **extract_link_service.public_settings(),
                **paypal_payment_service.public_settings(),
                **cdk_web_backend.public_settings(),
            },
            "payment_settings": paypal_payment_service.public_settings(),
            "queue": {"extract": extract_link_service.queue_settings(), "payment": paypal_payment_service.queue_settings(), "cdk_web": cdk_web_backend.queue_settings()},
        })

    @app.post("/api/paypal-protocol/settings")
    def api_paypal_protocol_settings_update():
        """更新提链/协议支付运行设置；密钥和代理只写入 .env，不回显。"""
        data = request.get_json(silent=True) or {}
        updates = {}
        if "auto_extract" in data or "enabled" in data:
            value = data.get("auto_extract", data.get("enabled"))
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "on", "y"}
            if not isinstance(value, bool):
                return jsonify({"ok": False, "error": "auto_extract 必须是布尔值"}), 400
            updates["EXTRACT_LINK_AUTO"] = value
        if "proxy" in data or "default_proxy" in data:
            proxy = data.get("proxy", data.get("default_proxy"))
            if proxy is None:
                proxy = ""
            if not isinstance(proxy, str) or len(proxy) > 2000:
                return jsonify({"ok": False, "error": "proxy 必须是字符串且长度不超过 2000"}), 400
            updates["EXTRACT_LINK_PROXY"] = proxy.strip()
        # PayPal 协议支付设置。页面不会发送 service，PayPal 服务码由配置默认值维护。
        bool_fields = {
            "auto_payment": "PAYPAL_PAYMENT_AUTO",
            "payment_auto": "PAYPAL_PAYMENT_AUTO",
            "service_autostart": "PAYPAL_PAYMENT_AUTOSTART_SERVICE",
            "cdk_web_enabled": "CDK_WEB_ENABLED",
            "cdk_enabled": "CDK_WEB_ENABLED",
            "cdk_auto_payment": "CDK_WEB_AUTO_PAYMENT",
        }
        for source_key, target_key in bool_fields.items():
            if source_key not in data:
                continue
            value = data.get(source_key)
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "on", "y"}
            if not isinstance(value, bool):
                return jsonify({"ok": False, "error": f"{source_key} 必须是布尔值"}), 400
            updates[target_key] = value
        if "cdk_web_enabled" in data or "cdk_enabled" in data:
            cdk_value = data.get("cdk_web_enabled", data.get("cdk_enabled"))
            if isinstance(cdk_value, str):
                cdk_value = cdk_value.strip().lower() in {"1", "true", "yes", "on", "y"}
            if cdk_value is True and "extract_backend" not in data and "backend" not in data:
                updates["EXTRACT_LINK_BACKEND"] = "cdk_web"
            elif cdk_value is False and "extract_backend" not in data and "backend" not in data:
                try:
                    if extract_link_service.backend_name() == "cdk_web":
                        updates["EXTRACT_LINK_BACKEND"] = "local"
                except Exception:
                    pass
        string_fields = {
            "extract_backend": "EXTRACT_LINK_BACKEND",
            "backend": "EXTRACT_LINK_BACKEND",
            "payment_country": "PAYPAL_PAYMENT_COUNTRY",
            "payment_proxy": "PAYPAL_PAYMENT_PROXY",
            "default_payment_proxy": "PAYPAL_PAYMENT_PROXY",
            "service_base": "PAYPAL_PAYMENT_SERVICE_BASE",
            "payment_project_path": "PAYPAL_PAYMENT_PROJECT_PATH",
            "sms_country": "PAYPAL_PAYMENT_SMS_COUNTRY",
            "sms_provider_ids": "PAYPAL_PAYMENT_SMS_PROVIDER_IDS",
            "sms_api_base": "PAYPAL_PAYMENT_SMS_API_BASE",
            "sms_api_key": "PAYPAL_PAYMENT_SMS_API_KEY",
            "cdk_web_base_url": "CDK_WEB_BASE_URL",
            "cdk_workbench_password": "CDK_WEB_WORKBENCH_PASSWORD",
            "cdk_country": "CDK_WEB_COUNTRY",
            "cdk_protocol_country": "CDK_WEB_PROTOCOL_COUNTRY",
            "cdk_sms_mode": "CDK_WEB_SMS_MODE",
            "cdk_sms_provider": "CDK_WEB_SMS_PROVIDER",
            "cdk_sms_api_key": "CDK_WEB_SMS_API_KEY",
            "cdk_sms_country": "CDK_WEB_SMS_COUNTRY",
        }
        for source_key, target_key in string_fields.items():
            if source_key not in data:
                continue
            value = data.get(source_key)
            if value is None:
                value = ""
            if not isinstance(value, str) or len(value) > 4000:
                return jsonify({"ok": False, "error": f"{source_key} 必须是长度不超过 4000 的字符串"}), 400
            updates[target_key] = value.strip()
        if "payment_country" in data or "country" in data:
            # The protocol project consumes an ISO-3166 alpha-2 billing
            # country.  Validate before writing so a malformed value cannot
            # make the subsequent settings response fail with a 500.
            candidate_country = str(data.get("payment_country", data.get("country", "")) or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", candidate_country):
                return jsonify({"ok": False, "error": "payment_country 必须是两位国家代码"}), 400
            updates["PAYPAL_PAYMENT_COUNTRY"] = candidate_country
        for source_key in ("cdk_country", "cdk_protocol_country"):
            if source_key not in data:
                continue
            candidate = str(data.get(source_key) or "").strip().upper()
            if not candidate and source_key == "cdk_protocol_country":
                updates["CDK_WEB_PROTOCOL_COUNTRY"] = ""
                continue
            if not re.fullmatch(r"[A-Z]{2}", candidate):
                return jsonify({"ok": False, "error": f"{source_key} 必须是两位国家代码"}), 400
            updates["CDK_WEB_COUNTRY" if source_key == "cdk_country" else "CDK_WEB_PROTOCOL_COUNTRY"] = candidate
        if "extract_backend" in data or "backend" in data:
            candidate_backend = str(data.get("extract_backend", data.get("backend", "")) or "").strip().lower()
            if candidate_backend in {"cdk", "1k50", "web", "cdk-web"}:
                candidate_backend = "cdk_web"
            if candidate_backend not in {"local", "remote", "cdk_web"}:
                return jsonify({"ok": False, "error": "extract_backend 仅支持 local / remote / cdk_web"}), 400
            updates["EXTRACT_LINK_BACKEND"] = candidate_backend
        int_fields = {
            "sms_timeout": ("PAYPAL_PAYMENT_SMS_TIMEOUT", 20, 3600),
            "payment_retries": ("PAYPAL_PAYMENT_MAX_RETRIES", 0, 20),
            "cdk_retries": ("CDK_WEB_MAX_RETRIES", 0, 20),
        }
        for source_key, (target_key, lower, upper) in int_fields.items():
            if source_key not in data:
                continue
            try:
                value = int(data.get(source_key))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"{source_key} 必须是整数"}), 400
            if not lower <= value <= upper:
                return jsonify({"ok": False, "error": f"{source_key} 范围为 {lower}-{upper}"}), 400
            updates[target_key] = value
        if not updates:
            return jsonify({"ok": False, "error": "没有可更新的设置"}), 400
        try:
            result = config_editor.update_config(updates)
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            logger.exception("Paypal协议设置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}), 500
        return jsonify({
            "ok": True,
            "updated": result.get("updated", []),
            # `config_editor` canonicalizes the paired CDK/backend values
            # before persisting.  Return that decision explicitly so callers
            # never have to infer which of two conflicting inputs won.
            "mode": result.get("mode") or extract_link_service.mode_state(),
            "settings": {
                **extract_link_service.public_settings(),
                **paypal_payment_service.public_settings(),
                **cdk_web_backend.public_settings(),
            },
            "payment_settings": paypal_payment_service.public_settings(),
        })

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json
        from pathlib import Path as _Path

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        auth_path = str(acc.get("codex_agent_auth_path") or "").strip()
        if auth_path:
            p = _Path(auth_path)
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8"), p.name or filename

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/email-pool/recent-messages")
    def api_email_pool_recent_messages():
        """按邮箱池来源读取一条邮箱的最近邮件（纯文本、无凭据）。"""

        def response(payload: dict, status: int = 200):
            result = jsonify(payload)
            result.status_code = status
            result.headers["Cache-Control"] = "no-store, max-age=0"
            result.headers["Pragma"] = "no-cache"
            result.headers["Expires"] = "0"
            return result

        try:
            result = recent_mail_service.fetch_recent_messages(
                email=request.args.get("email"),
                source=request.args.get("source"),
                limit=request.args.get("limit"),
            )
            return response({"ok": True, **result})
        except recent_mail_service.RecentMailValidationError as exc:
            return response({"ok": False, "error": str(exc)}, 400)
        except recent_mail_service.RecentMailNotFoundError as exc:
            return response({"ok": False, "error": str(exc)}, 404)
        except recent_mail_service.RecentMailFetchError as exc:
            return response({"ok": False, "error": str(exc)}, 502)

    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        # token=1 → 只看没有 access_token 的邮箱（无Token）；token=0 → 只看有 token 的
        token_filter = request.args.get("token") or None
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or q or token_filter) else limit
        if source == "all":
            rows = []
            rows += _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
            rows += _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
            rows += _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
            rows += _with_pool_source(db.list_imap_email_pool(status=status, limit=fetch_limit), "imap_pass")
            rows += _with_pool_source(db.list_mailcom_email_pool(status=status, limit=fetch_limit), "mailcom")
            rows = sorted(rows, key=lambda x: str(x.get("created_at") or x.get("imported_at") or x.get("used_at") or ""), reverse=True)
        elif source == "generic_api":
            rows = _with_pool_source(db.list_generic_api_email_pool(status=status, limit=fetch_limit), "generic_api")
        elif source == "cloudflare_domain":
            rows = _with_pool_source(db.list_domain_email_pool(status=status, limit=fetch_limit), "cloudflare_domain")
        elif source == "imap_pass":
            rows = _with_pool_source(db.list_imap_email_pool(status=status, limit=fetch_limit), "imap_pass")
        elif source == "mailcom":
            rows = _with_pool_source(db.list_mailcom_email_pool(status=status, limit=fetch_limit), "mailcom")
        else:
            rows = _with_pool_source(db.list_outlook_pool(status=status, limit=fetch_limit), "outlook")
        if token_filter:
            # token=1/yes/no/无 → 只看没有 access_token 的（无Token）
            # token=0/true/有 → 只看有 access_token 的（有Token）
            show_no_token = str(token_filter) in ("1", "true", "yes", "no", "无")
            want_has_token = not show_no_token
            rows = [r for r in rows if bool(str(r.get("access_token") or "").strip()) == want_has_token]
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            return jsonify(_paginate_items(rows, page=page, page_size=page_size))
        return jsonify(rows[:limit])

    @app.get("/api/outlook/imap-hosts")
    def api_outlook_imap_hosts():
        """返回 imap 邮箱池里已有的服务商地址，供导入模态框下拉复用。"""
        return jsonify({"ok": True, "hosts": db.imap_email_hosts()})

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API：email----code_url
        mail.com / GMX / Caramail：email----password
        带 token 的通用 API：email---token---code_url（token 丢弃）
        分隔符兼容 ---- 与 ====。
        """
        data = request.get_json(silent=True) or {}
        source = (data.get("source") or data.get("type") or "").strip().lower()
        source = {
            "gmx": "mailcom",
            "gmx.com": "mailcom",
            "caramail": "mailcom",
            "caramail.com": "mailcom",
        }.get(source, source)
        text = data.get("text") or ""
        imap_host = (data.get("imap_host") or data.get("provider_host") or "").strip()
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        tokenized_lines = [_parse_tokenized_generic_api_line(line) for line in lines]
        # 保留 Outlook 入口对旧三段格式的自动识别；显式选择 mail.com/GMX/IMAP/xbovo
        # 时以用户选定来源为准，避免密码中带 ``---https://`` 被误判为通用 API。
        if source in ("", "outlook", "generic_api") and lines and all(
            record is not None for record in tokenized_lines
        ):
            source = "generic_api"
        # xbovo（iCloud Hide My Email）与通用 API 同池：邮箱----alias_xxx（第二段是 API Key）
        if source not in ("outlook", "generic_api", "xbovo", "imap_pass", "mailcom"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook / 通用 API / xbovo / IMAP / mail.com/GMX"}), 400
        as_registered = bool(data.get("as_registered", False))
        records = []
        converted = 0
        for line, tokenized_record in zip(lines, tokenized_lines):
            if source == "generic_api" and tokenized_record is not None:
                records.append(tokenized_record)
                converted += 1
                continue
            if source == "mailcom":
                delimiter = "----" if "----" in line else "====" if "====" in line else ""
                if not delimiter:
                    continue
                email_raw, password_raw = line.split(delimiter, 1)
                email_part = db.clean_pool_email_part(email_raw)
                password_part = db.clean_mailcom_password_part(password_raw)
                if not email_part or not password_part:
                    continue
                records.append({"email": email_part, "password": password_part})
                continue
            parts = line.split("----") if "----" in line else line.split("====")
            parts = [p.strip() for p in parts]
            if source in ("generic_api", "xbovo"):
                if len(parts) < 2:
                    continue
                records.append({
                    "email": parts[0],
                    "code_url": parts[1],
                    "access_token": parts[2] if len(parts) > 2 else "",
                    "totp_secret": parts[3] if len(parts) > 3 else "",
                })
                continue
            if source == "imap_pass":
                # 兼容 "邮箱 xxxx@xx.com----密码123456" 或 "邮箱----密码----服务商地址" 带中文标签的粘贴：
                # 按 ---- 分段后再分别清洗邮箱/密码/服务商地址段
                if len(parts) < 2:
                    continue
                email_part = db.clean_pool_email_part(parts[0])
                password_part = db.clean_pool_password_part(parts[1])
                host_part = db.clean_pool_host_part(parts[2]) if len(parts) > 2 else imap_host
                if not email_part or not password_part:
                    continue
                records.append({
                    "email": email_part,
                    "password": password_part,
                    "imap_host": host_part or None,
                })
                continue
            if len(parts) < 4:
                continue
            records.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
                "access_token": parts[4] if len(parts) > 4 else "",
                "totp_secret": parts[5] if len(parts) > 5 else "",
            })
        if not records:
            need = {
                "generic_api": "邮箱----取码地址，或 邮箱---token---取码地址",
                "xbovo": "2 段：邮箱----alias_xxx（iCloud API Key）",
                "imap_pass": "2 段：邮箱----密码（标准 IMAP 直连取信）",
                "mailcom": "2 段：mail.com/GMX/Caramail 邮箱地址----登录密码",
            }.get(source, "4 段：email----password----clientId----refreshToken")
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
        if as_registered:
            inserted, skipped = db.import_registered_email_accounts(records, source=source)
        elif source in ("generic_api", "xbovo"):
            inserted, skipped = db.import_generic_api_emails(records)
        elif source == "imap_pass":
            inserted, skipped = db.import_imap_pass_emails(records, imap_host=imap_host)
        elif source == "mailcom":
            inserted, skipped = db.import_mailcom_emails(records)
        else:
            inserted, skipped = db.import_outlook_accounts(records)
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
            "as_registered": as_registered,
            "source": source,
            "converted": converted,
        })

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source == "generic_api":
            db.release_generic_api_email(email, status=status, note=data.get("note"))
        elif source == "cloudflare_domain":
            db.release_domain_email(email, status=status, note=data.get("note"))
        elif source == "imap_pass":
            db.release_imap_email(email, status=status, note=data.get("note"))
        elif source == "mailcom":
            db.release_mailcom_email(email, status=status, note=data.get("note"))
        else:
            db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        if status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                if item_source == "generic_api":
                    db.release_generic_api_email(email, status=status, note=note)
                elif item_source == "cloudflare_domain":
                    db.release_domain_email(email, status=status, note=note)
                elif item_source == "imap_pass":
                    db.release_imap_email(email, status=status, note=note)
                elif item_source == "mailcom":
                    db.release_mailcom_email(email, status=status, note=note)
                else:
                    db.release_outlook(email, status=status, note=note)
                updated.append({"email": email, "source": item_source, "status": status})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        deleted = (
            db.delete_generic_api_email(email)
            if source == "generic_api"
            else db.delete_imap_email(email)
            if source == "imap_pass"
            else db.delete_mailcom_email(email)
            if source == "mailcom"
            else db.delete_domain_email(email)
            if source == "cloudflare_domain"
            else db.delete_outlook(email)
        )
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {emails: [...]}。"""
        data = request.get_json(silent=True) or {}
        source = _pool_source_arg()
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[str] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            deleted_ok = (
                db.delete_generic_api_email(email)
                if item_source == "generic_api"
                else db.delete_imap_email(email)
                if item_source == "imap_pass"
                else db.delete_mailcom_email(email)
                if item_source == "mailcom"
                else db.delete_domain_email(email)
                if item_source == "cloudflare_domain"
                else db.delete_outlook(email)
            )
            if deleted_ok:
                deleted.append({"email": email, "source": item_source})
            else:
                skipped.append({"email": email, "reason": "邮箱不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete-all")
    def api_outlook_delete_all():
        """清空所选邮箱池；必须显式确认，并保留运行任务正在使用的邮箱。"""
        data = request.get_json(silent=True) or {}
        if data.get("confirm") is not True:
            return jsonify({"ok": False, "error": "必须传入 confirm: true 才能删除全部邮箱"}), 400
        source = str(data.get("source") or "all").strip().lower()
        if source not in ("all", "outlook", "generic_api", "cloudflare_domain", "imap_pass", "mailcom"):
            return jsonify({
                "ok": False,
                "error": "source 必须是 all / outlook / generic_api / cloudflare_domain / imap_pass / mailcom",
            }), 400
        result = db.delete_all_email_pool(source=source)
        return jsonify({"ok": True, **result})

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_domain_email(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_domain_email(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        rows = db.list_codex_accounts()
        q = str(request.args.get("q", default="") or "").strip()
        if q:
            rows = [r for r in rows if _matches_query(r, q)]
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": rows[:limit],
        })

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        if (acc.get("codex_status") or "") == "deactivated":
            return jsonify({"ok": False, "error": "账号已废号，不能补跑 Codex"}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 1-16}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 1)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = max(1, min(16, int(workers)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是数字"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            if (acc.get("codex_status") or "") == "deactivated":
                skipped.append({"id": acc_id, "email": email, "reason": "账号已废号"})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "正在补跑中"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        threading.Thread(
            target=_bulk_runner,
            args=(selected, workers, batch_id),
            name=f"codex-bulk-dispatch-{batch_id}",
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "message": f"已开始批量补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/sms/countries")
    def api_sms_countries():
        """拉接码平台（GrizzlySMS / SMSBower）国家列表，供配置页选择国家。"""
        from core import sms_provider
        countries = sms_provider.list_countries()
        return jsonify({"ok": True, "countries": countries, "count": len(countries)})

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活 / 2FA 补跑日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        running = live_check_service.is_checking(email) or twofa_service.is_running(email)
        p = account_liveness.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": running})
        max_bytes = 80_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": running,
        })

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        fetch_limit = 1_000_000 if (paged or page_arg is not None or page_size_arg is not None) else limit
        from config import email as _email_cfg
        manual_otp_required = not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
        rows = db.list_jobs(limit=fetch_limit)
        for row in rows:
            row["manual_otp_required"] = manual_otp_required
            row.update(svc.get_retry_info(row))
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = _paginate_items(rows, page=page, page_size=page_size)
            result["items"] = [_compact_job_for_list(r) for r in (result.get("items") or [])]
            result["status_counts"] = _job_status_counts(rows)
            result["compact"] = True
            return jsonify(result)
        return jsonify(rows)

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册：body {count, workers}。"""
        data = request.get_json(silent=True) or {}
        from core.registration_ip import normalize_country_code
        try:
            country_raw = data.get("registration_country") or data.get("country") or ""
            registration_country = normalize_country_code(
                country_raw,
                strict=bool(str(country_raw).strip()),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = max(1, min(16, int(data.get("workers", 3))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from config import register as _register_cfg
        from core.email_provider import parse_email_sources
        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
            reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            if count > 1:
                return jsonify({
                    "ok": False,
                    "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。",
                }), 400
            if registration_country:
                jobs = svc.submit_registration(
                    count=count,
                    workers=workers,
                    registration_country=registration_country,
                )
            else:
                jobs = svc.submit_registration(count=count, workers=workers)
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "jobs": jobs,
                "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
                "workers": workers,
                "registration_country": registration_country,
            })
        sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
        if "gptmail" in sources:
            api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudflare" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
            accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
            api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
            needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not api_key:
                return jsonify({
                    "ok": False,
                    "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "mailnest" in sources:
            api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
            project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if not project_code:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudmail" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
            token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
                }), 400
        if "gptmail" in sources or "mailnest" in sources or "cloudmail" in sources or "cloudflare" in sources:
            # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
            warning = ""
        elif "cloudflare_domain" in sources:
            pool = db.domain_email_pool_summary()
            warning = ""
            if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
                warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        elif sources == ["generic_api"]:
            pool = db.generic_api_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["imap_pass"]:
            pool = db.imap_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"IMAP 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["mailcom"]:
            pool = db.mailcom_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"mail.com / GMX 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif len(sources) > 1:
            available = 0
            if "outlook" in sources:
                available += db.outlook_pool_summary().get("available", 0)
            if "generic_api" in sources:
                available += db.generic_api_email_pool_summary().get("available", 0)
            if "imap_pass" in sources:
                available += db.imap_email_pool_summary().get("available", 0)
            if "mailcom" in sources:
                available += db.mailcom_email_pool_summary().get("available", 0)
            warning = ""
            if available < count:
                warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
        else:
            pool = db.outlook_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        if registration_country:
            jobs = svc.submit_registration(
                count=count,
                workers=workers,
                registration_country=registration_country,
            )
        else:
            jobs = svc.submit_registration(count=count, workers=workers)
        return jsonify({
            "ok": True,
            "submitted": len(jobs),
            "jobs": jobs,
            "warning": warning,
            "workers": workers,
            "registration_country": registration_country,
        })

    @app.get("/api/manual-otp/waiting")
    def api_manual_otp_waiting():
        """列出当前正在等待手动验证码的邮箱。"""
        from core.manual_otp import list_waiting
        return jsonify({"ok": True, "waiting": list_waiting()})

    @app.post("/api/manual-otp")
    def api_manual_otp_submit():
        """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
        from core.manual_otp import submit_manual_otp
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or data.get("otp") or "").strip()
        email = (data.get("email") or "").strip()
        job_id = data.get("job_id")
        if not email and job_id is not None:
            job = db.get_job(int(job_id))
            email = (job or {}).get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
        try:
            result = submit_manual_otp(email, code)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        result = svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("running", "stopping"):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            job = db.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "任务不存在"})
                continue
            if job.get("status") in ("running", "stopping"):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            if db.delete_job(job_id, delete_log=True, allow_running=False):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.post("/api/jobs/delete-all")
    def api_jobs_delete_all():
        """删除全部任务记录。running/stopping 任务跳过，其它任务删除记录和日志。"""
        jobs = db.list_jobs(limit=1_000_000)
        deleted: list[int] = []
        skipped: list[dict] = []
        for job in jobs:
            job_id = int(job.get("id") or 0)
            if not job_id:
                continue
            if job.get("status") in ("running", "stopping"):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            if db.delete_job(job_id, delete_log=True, allow_running=False):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "mode": result.get("mode") or extract_link_service.mode_state(),
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
