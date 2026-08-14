# -*- coding: utf-8 -*-
"""通过 CloakBrowser + Playwright 适配层执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from config import cloakbrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.registration_ip import (
    detect_selenium_registration_geo,
    registration_geo_from_open_result,
    registration_geo_matches,
)

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _click_continue_with_password, _clear_otp_inputs, _type_otp, _click_continue,
    _wait_after_email_otp_submit, _click_resend_email_otp, _complete_profile_page,
    _fetch_chatgpt_session, _check_manual_stop, _enable_2fa_with_retry, _add_password_post_signup,
    _register_set_password_enabled, _registration_password,
    _run_stage_with_recovery, _resend_email_otp_with_recovery, _registration_stage_state,
)

logger = logging.getLogger(__name__)


def run_cloak_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    registration_country: str = "",
) -> dict:
    """CloakBrowser 自动化注册入口。"""
    try:
        from core.registration_service import _run_max_minutes, set_run_deadline
        set_run_deadline(_run_max_minutes())
    except Exception:
        pass
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    registration_ip = ""
    try:
        driver, opened = build_cloak_driver(
            proxy=proxy,
            registration_country=registration_country,
            registration_preflight=True,
        )
        # build_cloak_driver has already measured the route inside this exact
        # browser and rotated bad candidates before any identity was submitted.
        registration_geo = registration_geo_from_open_result(opened.raw if opened else None)
        # Compatibility for injected/older driver builders used by integrations.
        if not registration_geo.get("ip"):
            registration_geo = detect_selenium_registration_geo(
                driver,
                log_prefix="[Cloak注册]",
                require_country=bool(registration_country),
            )
        route_ok, route_error = registration_geo_matches(registration_geo, registration_country)
        if not route_ok:
            raise RuntimeError(route_error)
        registration_ip = registration_geo["ip"]
        logger.info("[Cloak注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        auth_entry_url = "https://chatgpt.com/auth/login"
        logger.info("[Cloak注册] 打开登录页：%s", auth_entry_url)
        _run_stage_with_recovery(
            driver,
            "打开注册入口",
            lambda: driver.get(auth_entry_url),
            stage_url=auth_entry_url,
            resume_from_state=lambda state, _report: (state == "email", None),
        )
        human_delay("navigate")
        _maybe_accept(driver)
        _check_manual_stop()

        next_state = _run_stage_with_recovery(
            driver,
            "提交邮箱",
            lambda: _submit_email_and_wait_next(driver, email, attempts=3),
            stage_url=auth_entry_url,
            resume_from_state=lambda state, _report: (
                state in ("password", "otp", "profile", "chatgpt", "logged_in"),
                state,
            ),
        )
        _check_manual_stop()

        # 新版注册流：邮箱直达 OTP 页。若开启"注册时设密码"，先点 OTP 页的
        # "使用密码继续"切到 /create-account/password 填密码；不开则纯 OTP。
        openai_password = None
        advanced_state = next_state if next_state in ("profile", "chatgpt", "logged_in") else None
        password_stage_url = str(getattr(driver, "current_url", "") or "")

        def _replay_email_before_password() -> None:
            _maybe_accept(driver)
            _submit_email_and_wait_next(driver, email, attempts=2)

        def _run_password_stage():
            if next_state == "otp":
                if _register_set_password_enabled():
                    if _click_continue_with_password(driver, timeout=20):
                        return _fill_password_page_if_present(driver, email, timeout=30), None
                    logger.info("[Cloak注册][密码] 未找到'使用密码继续'入口，走纯 OTP 注册")
                return None, "otp"
            return _fill_password_page_if_present(driver, email, timeout=25), None

        if advanced_state is None:
            def _password_resume(state, _report):
                progressed = state in ("profile", "chatgpt", "logged_in") or (
                    next_state == "password" and state == "otp"
                )
                return progressed, (None, state if progressed else None)

            openai_password, recovered_state = _run_stage_with_recovery(
                driver,
                "处理密码节点",
                _run_password_stage,
                stage_url=password_stage_url,
                previous_url=auth_entry_url,
                replay_previous=_replay_email_before_password,
                resume_from_state=_password_resume,
            )
            if recovered_state in ("profile", "chatgpt", "logged_in"):
                advanced_state = recovered_state
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        otp_attempts = range(1, max_otp_attempts + 1) if advanced_state is None else ()
        for otp_attempt in otp_attempts:
            if current_otp is None:
                logger.info("[Cloak注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Cloak注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _resend_email_otp_with_recovery(driver, email, entry_url=auth_entry_url)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Cloak注册][OTP] 收到验证码：%s", current_otp)
            otp_stage_url = str(getattr(driver, "current_url", "") or "")

            def _prepare_otp_input():
                nonlocal openai_password
                state_before_otp = _registration_stage_state(driver)
                if state_before_otp in ("password", "login_password"):
                    logger.warning(
                        "[Cloak注册][恢复][OTP] 验证码输入前页面迟到进入密码节点，先恢复密码步骤：state=%s",
                        state_before_otp,
                    )
                    recovered_password = _fill_password_page_if_present(driver, email, timeout=30)
                    if recovered_password:
                        openai_password = recovered_password
                _clear_otp_inputs(driver)
                _type_otp(driver, current_otp)
                return "typed"

            otp_prepare_state = _run_stage_with_recovery(
                driver,
                "填写邮箱验证码",
                _prepare_otp_input,
                stage_url=otp_stage_url,
                resume_from_state=lambda state, _report: (
                    state in ("profile", "chatgpt", "logged_in"),
                    state,
                ),
            )
            if otp_prepare_state in ("profile", "chatgpt", "logged_in"):
                advanced_state = otp_prepare_state
                break
            human_delay("otp_input")
            try:
                _click_continue(driver)
            except Exception as exc:
                logger.info("[Cloak注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _run_stage_with_recovery(
                driver,
                "等待邮箱验证码提交结果",
                lambda: _wait_after_email_otp_submit(driver, timeout=30),
                stage_url=otp_stage_url,
                resume_from_state=lambda state, _report: (
                    state in ("profile", "chatgpt", "logged_in"),
                    "accepted",
                ),
            )
            if outcome == "accepted":
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            otp_after_ts = time.time()
            _resend_email_otp_with_recovery(driver, email, entry_url=auth_entry_url)
            human_delay("api")
            current_otp = None

        profile_stage_url = str(getattr(driver, "current_url", "") or "")
        profile_submitted = _run_stage_with_recovery(
            driver,
            "完成资料节点",
            lambda: _complete_profile_page(driver, name, birthday, timeout=60),
            stage_url=profile_stage_url,
            resume_from_state=lambda state, _report: (
                state in ("chatgpt", "logged_in"),
                False,
            ),
        )
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _run_stage_with_recovery(
            driver,
            "读取登录会话",
            lambda: _fetch_chatgpt_session(driver, timeout=120),
            stage_url="https://chatgpt.com/",
        )
        access_token = session_info["accessToken"]
        logger.info("[Cloak注册] 已拿到 accessToken：%s", email)

        # 注册完成、账号已创建后：若注册时未在 password 页设过密码（新邮箱直达 OTP 页，
        # 无 password 页），则用 signup 会话的 post_login_add_password 标志在浏览器内补设密码。
        # 这是"账号从出生就带密码"的关键：既账号补密码协议被证伪，但新号会话允许设密码。
        if not openai_password and _register_set_password_enabled():
            _pwd = _registration_password()
            logger.info("[Cloak注册][密码] 注册流程未走 password 页，尝试 signup 会话补设密码（%s 位）", len(_pwd))
            if _add_password_post_signup(driver, email, _pwd):
                openai_password = _pwd
                logger.info("[Cloak注册][密码] signup 会话补设密码成功")
            else:
                logger.warning("[Cloak注册][密码] signup 会话补设密码未完成（不影响注册成功）")
            _check_manual_stop()

        totp_secret = None
        if _twofa_cfg.ENABLE_2FA:
            # 复用 Roxy 的浏览器内 TOTP 设置链路（execute_async_script + fetch），
            # 全程使用当前浏览器会话/出口 IP/UA，与注册一致。失败不影响注册成功。
            logger.info("[Cloak注册][2FA] 启用 2FA，开始设置 TOTP（会再收一封 OTP 邮件）")
            _2fa = _enable_2fa_with_retry(driver, email)
            if _2fa:
                totp_secret, fresh_token = _2fa
                if fresh_token:
                    access_token = fresh_token
                logger.info("[Cloak注册][2FA] TOTP 设置完成，secret=%s...%s", totp_secret[:4], totp_secret[-4:])
            else:
                logger.warning("[Cloak注册][2FA] TOTP 设置未完成，继续保存账号")

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 CloakBrowser 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Cloak注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            registration_ip=registration_ip or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "cloakbrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
            },
        )
        # 注册时设置了密码（REGISTER_SET_PASSWORD=True）→ 写 chatgpt_password 字段，
        # 让发货格式 邮箱----密码----2FA----AT 能带上密码。
        if openai_password:
            try:
                from core import db as _db
                from datetime import datetime as _dt
                _db.update_account_password(account_id, {
                    "password": openai_password,
                    "password_status": "success",
                    "password_error": None,
                    "password_done_at": _dt.now().isoformat(timespec="seconds"),
                })
                logger.info("[Cloak注册] 注册密码已落库 chatgpt_password 字段")
            except Exception as exc:
                logger.warning("[Cloak注册] 密码落库失败（不影响注册）: %s", str(exc)[:160])
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {"success": bool(codex_ok), "email": email, "account_id": account_id, "access_token": access_token, "totp_secret": totp_secret, "password": openai_password, "codex": codex_result, "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}"}
    except Exception as exc:
        logger.error("[Cloak注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Cloak注册] 失败详情", exc_info=True)
        try:
            from core.email_provider import release_email_if_unconsumed
            release_email_if_unconsumed(email, note=f"Cloak注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {"success": False, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if driver and not bool(_cfg.CLOAK_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
