# -*- coding: utf-8 -*-
"""新协议注册引擎接入层。

把源项目（gpt-outlook-register）的 AuthFlow 纯协议引擎接到本项目现有的
任务系统 / 邮箱池 / 账号存储 / 2FA / Codex / Flow 上：

    本项目 job(_run_one_job) ──> run_protocol_registration(email, ...)
                                    │
                                    ├─ 国家过滤（保留本项目注册国家功能）
                                    ├─ 构建源引擎 Config(proxy/upstream)
                                    ├─ 构建本项目邮箱来源的 MailProvider 适配器
                                    ├─ AuthFlow.run_register(mail)
                                    └─ 结果映射 → save_account_data / 2FA / Codex / Flow

说明：
  - 邮箱领取与失败回收仍由本项目任务系统负责（本模块不改邮箱池状态）。
  - 源引擎始终为账号设置登录密码（新号 register_password），与
    config.register.REGISTER_SET_PASSWORD=True 的行为一致。
  - 手动 OTP 输入（otp_code）不支持：源引擎用 mail_provider.wait_for_otp 收码，
    需 USE_EMAIL_SERVICE=True。
"""
from __future__ import annotations

import logging
import random
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_MAX_PROXY_ATTEMPTS = 4

# 每个 worker 线程各自的回调暂存（on_password / 2FA），避免多 worker 串值。
_CTX = threading.local()


def _ctx_get(key: str, default=None):
    return getattr(_CTX, key, default)


def _ctx_set(key: str, value) -> None:
    setattr(_CTX, key, value)


def _ctx_clear(*keys: str) -> None:
    for key in keys:
        if hasattr(_CTX, key):
            delattr(_CTX, key)


def _upstream_from_config():
    """本项目 PROXY_PRE_PROXY → 源引擎 upstream；留空则交给源引擎自动检测系统代理。"""
    try:
        from config import proxy as _proxy_cfg
        explicit = str(getattr(_proxy_cfg, "PROXY_PRE_PROXY", "") or "").strip()
    except Exception:
        explicit = ""
    try:
        from protocol_engine.http_client import resolve_upstream_proxy
        return resolve_upstream_proxy(explicit or None)
    except Exception:
        return explicit or None


def _pick_registration_proxy(registration_country: str = "") -> str | None:
    """从 PROXY_POOL 选代理；有国家目标时做出口 IP 国家预检/轮换（≤4 次）。"""
    from config import proxy as _proxy_cfg
    from core.registration_ip import (
        detect_http_registration_geo,
        registration_geo_matches,
    )
    from protocol_engine.http_client import create_http_session

    pool = [p for p in (getattr(_proxy_cfg, "PROXY_POOL", None) or []) if str(p).strip()]
    if not pool:
        return None
    if not registration_country:
        return random.choice(pool)

    upstream = _upstream_from_config()
    last_err = ""
    tried: set[str] = set()
    for attempt in range(1, _MAX_PROXY_ATTEMPTS + 1):
        candidates = [p for p in pool if p not in tried] or pool
        proxy = random.choice(candidates)
        tried.add(proxy)
        session = None
        try:
            session = create_http_session(proxy=proxy, upstream=upstream)
            geo = detect_http_registration_geo(
                session,
                log_prefix="[注册][代理预检]",
                require_country=True,
            )
            ok, err = registration_geo_matches(geo, registration_country)
            if ok:
                logger.info(
                    "[注册][代理预检] 第 %s 次命中：IP=%s country=%s target=%s",
                    attempt, geo.get("ip") or "?", geo.get("country") or "?", registration_country,
                )
                return proxy
            last_err = err
            logger.warning(
                "[注册][代理预检] 实测 country=%s 不匹配目标 %s，换代理：%s",
                geo.get("country") or "?", registration_country, err,
            )
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            logger.warning("[注册][代理预检] %s 预检失败：%s", proxy, last_err)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
    raise RuntimeError(
        f"代理出口国家预检未命中（{_MAX_PROXY_ATTEMPTS} 次）：{last_err}"
    )


def _build_mail_settings(source: str) -> dict:
    """从本项目 config.email 提取邮箱来源配置（供 provider from_config 使用）。"""
    settings = {"source": source}
    try:
        from config import email as _email_cfg
        for key in ("OTP_MAX_WAIT", "OTP_POLL_INTERVAL", "OTP_SETTLE_SECONDS"):
            settings[key] = getattr(_email_cfg, key, None)
    except Exception:
        pass
    return settings


def _account_callback(email: str) -> dict:
    """供源引擎登录路径用：从本项目账号库回读密码与 totp_secret。"""
    try:
        from core import db
        acc = db.get_account_by_email(email) or {}
        return {
            "password": str(acc.get("chatgpt_password") or ""),
            "totp_secret": str(acc.get("totp_secret") or ""),
        }
    except Exception:
        return {}


class _EngineSessionAdapter:
    """把源引擎 AuthFlow 的 curl_cffi session 包成 BrowserSession 接口，
    供 core.chatgpt_bootstrap 预热复用（共享同一 cookie jar / TLS 指纹）。
    """

    def __init__(self, flow):
        self._flow = flow
        self.session = flow.session

    def get(self, url, headers=None, **kw):
        kw.setdefault("timeout", 25)
        return self.session.get(url, headers=headers, **kw)

    def post(self, url, headers=None, data=None, **kw):
        kw.setdefault("timeout", 25)
        return self.session.post(url, headers=headers, data=data, **kw)

    def get_chatgpt_headers(self, referer: str = "https://chatgpt.com/"):
        return self._flow._common_headers(referer)

    def js_timezone_offset_min(self) -> int:
        try:
            from config import browser as _browser_cfg
            return -int(getattr(_browser_cfg, "TIMEZONE_OFFSET_MINUTES", 0) or 0)
        except Exception:
            return -480

    @property
    def device_id(self):
        try:
            did = str(getattr(self._flow, "device_id", "") or "")
            if did:
                return did
            return str(self.session.cookies.get("oai-did", "") or "")
        except Exception:
            return ""

    @property
    def sentinel_sid(self):
        return self.device_id

    browser_profile = None


def _bootstrap_anonymous(flow) -> None:
    """源引擎 warmup 种好 cookie 后、进入 CSRF 前跑匿名态 ChatGPT 首屏预热。"""
    from config import openai_protocol as _proto_cfg
    if not bool(getattr(_proto_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", True)):
        return
    try:
        from core.chatgpt_bootstrap import anonymous_bootstrap
        anonymous_bootstrap(
            _EngineSessionAdapter(flow),
            strict=bool(getattr(_proto_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
        )
    except Exception as exc:
        logger.warning("[Bootstrap] 匿名预热异常（不影响注册）: %s", exc)


def _bootstrap_authenticated(flow, access_token: str) -> None:
    """注册拿到 access_token 后跑登录态 ChatGPT 首屏预热。"""
    from config import openai_protocol as _proto_cfg
    if not bool(getattr(_proto_cfg, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", True)):
        return
    try:
        from core.chatgpt_bootstrap import authenticated_bootstrap
        authenticated_bootstrap(
            _EngineSessionAdapter(flow),
            access_token,
            strict=bool(getattr(_proto_cfg, "CHATGPT_BOOTSTRAP_STRICT", False)),
        )
    except Exception as exc:
        logger.warning("[Bootstrap] 登录态预热异常（不影响注册）: %s", exc)


def _on_password_cb(email: str, password: str) -> None:
    _ctx_set("password", password)


def _make_2fa_hook():
    """返回 on_session_ready 钩子：拿到 session 后、Codex 之前绑 2FA。"""
    def _hook(flow, at: str) -> None:
        if _ctx_get("tfa_secret"):
            return
        try:
            from protocol_engine.two_factor import bind_totp_2fa_inline
            info = bind_totp_2fa_inline(flow, at)
            if info and info.get("secret"):
                _ctx_set("tfa_secret", info["secret"])
                logger.info("[注册] 2FA 已绑定（注册会话快路径）")
        except Exception as exc:
            logger.warning("[注册] 2FA 钩子异常（账号仍有效）: %s", exc)
    return _hook


def run_protocol_registration(
    email: str,
    name: str = "",
    birthday: str | None = None,
    registration_country: str = "",
    proxy: str | None = None,
) -> dict:
    """源引擎完整注册，返回本项目 _run_one_job 期望的结果 dict。"""
    from config import email as _email_cfg
    from config import register as _register_cfg
    from config import twofa as _twofa_cfg
    from core import db
    from core.email_provider import resolve_email_source
    from core.profile_utils import generate_random_birthday
    from protocol_engine.auth_flow import AuthFlow
    from protocol_engine.config import Config
    from protocol_engine.mail_providers import create_mail_provider
    from protocol_engine.mail_providers.my_adapters import SOURCE_TO_KIND

    # 每次运行前清空线程暂存，避免线程池复用串值
    _ctx_clear("password", "tfa_secret")

    if not proxy:
        proxy = _pick_registration_proxy(registration_country)
    if not birthday:
        birthday = generate_random_birthday()

    source = resolve_email_source(email)
    kind = SOURCE_TO_KIND.get(source) or "generic_api"
    mail = create_mail_provider(
        kind,
        _build_mail_settings(source),
        {"email": email, "kind": kind},
    )

    cfg = Config(proxy=proxy, upstream=_upstream_from_config())

    env_overrides = {
        "OTP_TIMEOUT": str(int(getattr(_email_cfg, "OTP_MAX_WAIT", 90) or 90)),
    }
    # 号池邮箱被 OpenAI 识别为"已有账号"时的处理：
    #   WEBUI_ALLOW_LOGIN=1 → 走 OTP 登录拿已有账号凭证；不设 → 快速标死换下一个号。
    # 注册模式默认不设（false），避免在二手号上白等 90s；fetch-token 模式可手动开。
    if bool(getattr(_email_cfg, "WEBUI_ALLOW_LOGIN", False)):
        env_overrides["WEBUI_ALLOW_LOGIN"] = "1"
    # chatgpt.com 请求补真实前端上报头（引擎 _common_headers 读取）
    try:
        from config import openai_protocol as _proto_cfg
        _build_no = str(getattr(_proto_cfg, "OAI_CLIENT_BUILD_NUMBER", "") or "").strip()
        _ver = str(getattr(_proto_cfg, "OAI_CLIENT_VERSION", "") or "").strip()
        if _build_no:
            env_overrides["OAI_CLIENT_BUILD_NUMBER"] = _build_no
        if _ver:
            env_overrides["OAI_CLIENT_VERSION"] = _ver
    except Exception:
        pass
    skip_oauth = not bool(getattr(_register_cfg, "ENABLE_OAUTH_RT", True))
    if skip_oauth:
        env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
        env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"

    enable_2fa = bool(getattr(_twofa_cfg, "ENABLE_2FA", False))
    flow = AuthFlow(
        cfg,
        env_overrides=env_overrides,
        on_password=_on_password_cb,
        on_session_ready=_make_2fa_hook() if enable_2fa else None,
        account_callback=_account_callback,
        on_warmup_done=_bootstrap_anonymous,
    )
    # 注入姓名/生日（源 create_account 默认随机生成；有值则用之）
    flow._display_name = (name or "").strip() or None
    flow._birthdate = (birthday or "").strip() or None

    logger.info(
        "[注册] 新协议引擎：source=%s kind=%s email=%s proxy=%s target=%s",
        source, kind, email, proxy or "直连", registration_country or "不限",
    )

    d: dict
    try:
        result = flow.run_register(mail)
        d = result.to_dict()
    except RuntimeError as exc:
        d = flow.result.to_dict()
        if not (d.get("access_token") or d.get("refresh_token") or d.get("session_token")):
            raise
        logger.warning("[注册] 流程末段异常但已拿到部分凭证：%s", exc)

    access_token = d.get("access_token") or ""
    openai_password = _ctx_get("password") or d.get("password") or ""
    totp_secret = _ctx_get("tfa_secret") or d.get("totp_secret") or ""

    # ── 登录态 ChatGPT 首屏预热（提高账号信任度/Plus 试用资格命中）──
    if access_token:
        _bootstrap_authenticated(flow, access_token)

    # ── 2FA 兜底：钩子没跑到（异常走 partial 分支 / at 当时为空）时再补一次 ──
    if enable_2fa and not totp_secret:
        try:
            from protocol_engine.two_factor import bind_totp_2fa_inline
            info = bind_totp_2fa_inline(flow, access_token)
            if info and info.get("secret"):
                totp_secret = info["secret"]
        except Exception as exc:
            logger.warning("[注册] 2FA 兜底绑定异常: %s", exc)
    if enable_2fa and totp_secret:
        logger.info("[注册] 2FA 绑定成功：%s", email)

    # ── Codex OAuth（CPA 授权，沿用本项目链路）──
    codex_result = {"status": "skipped", "ok": False, "message": "未触发"}
    try:
        from core.codex_oauth import run_codex_oauth
        codex_result = run_codex_oauth(email)
    except Exception as exc:
        codex_result = {
            "status": "failed",
            "ok": False,
            "message": f"{type(exc).__name__}: {str(exc)[:180]}",
        }

    # ── 落库 ──
    account_id = _save_account(
        email, access_token, totp_secret, openai_password, source, proxy, codex_result, d
    )
    if openai_password:
        try:
            db.update_account_password(account_id, {
                "password": openai_password,
                "password_status": "success",
                "password_error": None,
                "password_done_at": datetime.now().isoformat(timespec="seconds"),
            })
            logger.info("[注册] 注册密码已落库 chatgpt_password 字段")
        except Exception as exc:
            logger.warning("[注册] 密码落库失败（不影响账号保存）: %s", str(exc)[:160])

    logger.info("[完成] %s，账号ID=%s，Token=%s...", email, account_id, (access_token or "")[:16])

    # ── Flow 触发 ──
    flow_result = {"status": "skipped", "ok": False, "message": "未触发"}
    try:
        from core.flow_trigger import trigger_flow
        flow_result = trigger_flow(access_token)
    except Exception as exc:
        flow_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}

    codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
    task_error = None
    if not codex_ok:
        task_error = f"Codex 未完成: {codex_result.get('message', '未知')}"
        logger.warning("[任务结果] %s 账号已保存但任务标失败，原因: %s", email, task_error)

    return {
        "success": bool(codex_ok),
        "email": email,
        "account_id": account_id,
        "access_token": access_token,
        "totp_secret": totp_secret,
        "password": openai_password,
        "flow": flow_result,
        "codex": codex_result,
        "error": task_error,
    }


def _save_account(
    email: str,
    access_token: str,
    totp_secret: str,
    password: str,
    source: str,
    proxy: str | None,
    codex_result: dict,
    d: dict,
) -> int:
    from core.account_export import save_account_data
    return save_account_data(
        email=email,
        access_token=access_token,
        totp_secret=totp_secret or None,
        email_source=source,
        proxy_used=proxy,
        registration_ip=None,
        extra={
            "device_id": d.get("device_id") or "",
            "session_token": d.get("session_token") or "",
            "refresh_token": d.get("refresh_token") or "",
            "id_token": d.get("id_token") or "",
            "cookie_header": d.get("cookie_header") or "",
            "csrf_token": d.get("csrf_token") or "",
            "registration_password": password,
            "codex": codex_result,
        },
    )
