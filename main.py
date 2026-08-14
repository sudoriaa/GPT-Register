# -*- coding: utf-8 -*-
"""
ChatGPT 协议注册全流程入口
串联 12 个步骤，自动完成 ChatGPT 账号注册
"""
import sys
import argparse
import logging
import random
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from config import REGISTER_EMAIL, REGISTER_NAME  # 这两个一般不在 WebUI 改
# 可热改的，按模块属性方式读
from config import twofa as _twofa_cfg
from config import email as _email_cfg
from config import register as _register_cfg
from config import roxybrowser as _roxy_cfg
from config import openai_protocol as _protocol_cfg
from core.session import BrowserSession
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    get_create_account_page,
    register_user,
    follow_password_registration_continue,
    request_sentinel_token,
    build_sentinel_header,
    validate_email_otp,
    send_email_otp,
    network_preflight,
    navigate_about_you,
    EmailOtpInvalidError,
    create_account,
)
from core.account_export import (
    follow_oauth_callback,
    fetch_session,
    setup_2fa,
    save_account_data,
    create_batch_archive_dir,
)
from core.email_provider import acquire_email, wait_for_otp
from core.humanize import delay as human_delay
from core.name_samples import random_display_name
from core.profile_utils import generate_random_birthday

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_FINALIZE_SESSION_MAX_ATTEMPTS = 5
_FINALIZE_SESSION_BACKOFF_BASE = 2.0
_REGISTRATION_PROXY_MAX_ATTEMPTS = 4
_REGISTRATION_PROXY_ERROR_HINTS = (
    "proxy", "socks", "tunnel", "timeout", "timed out", "connection",
    "curl: (5", "curl: (6", "curl: (7", "curl: (28", "curl: (35",
    "curl: (52", "curl: (56", "dns", "name resolution",
    "status=403", "status=408", "status=425", "status=429",
    "status=500", "status=502", "status=503", "status=504",
)


def _generate_protocol_password() -> str:
    """Generate the same 14-character password shape used by Roxy."""
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghjkmnpqrstuvwxyz"
    digits = "23456789"
    symbols = "!@#$%^&*"
    groups = (upper, lower, digits, symbols)
    pool = "".join(groups)
    chars = [random.choice(group) for group in groups]
    chars.extend(random.choice(pool) for _ in range(14 - len(chars)))
    random.shuffle(chars)
    return "".join(chars)


def _protocol_registration_password() -> str:
    configured = str(getattr(_register_cfg, "REGISTER_PASSWORD", "") or "").strip()
    return configured or _generate_protocol_password()


def _protocol_set_password_enabled() -> bool:
    return bool(getattr(_register_cfg, "REGISTER_SET_PASSWORD", True))


def _setup_protocol_2fa_with_retry(
    session: BrowserSession,
    email: str,
) -> str | None:
    """Run the full protocol reauth/enroll/activate flow on every attempt."""
    max_attempts = max(
        1,
        int(getattr(_twofa_cfg, "TWOFA_MAX_ATTEMPTS", 3) or 3),
    )
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        logger.info("[2FA] 开始完整设置流程（第 %s/%s 次）", attempt, max_attempts)
        try:
            secret = setup_2fa(session, email)
            if secret:
                if attempt > 1:
                    logger.info("[2FA] 第 %s 次重试后设置成功", attempt)
                return secret
            last_error = "设置流程未返回 TOTP secret"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            logger.warning(
                "[2FA] 第 %s/%s 次设置失败：%s",
                attempt,
                max_attempts,
                last_error,
            )
            logger.debug("2FA 错误详情:", exc_info=True)
        if attempt < max_attempts:
            delay = random.uniform(2.5, 5.0)
            logger.info("[2FA] %.1f 秒后从重认证节点重新开始", delay)
            time.sleep(delay)

    logger.warning(
        "[2FA] 连续 %s 次设置失败，账号仍会保存（不含 TOTP secret）：%s",
        max_attempts,
        last_error or "未知错误",
    )
    return None


def _is_registration_proxy_error(exc: BaseException) -> bool:
    """Return whether a failure happened before any business identity step."""
    text = f"{type(exc).__name__}: {exc}".lower()
    terminal = (
        "invalid otp", "incorrect otp", "验证码错误", "验证码过期",
        "invalid email", "already registered", "account disabled",
        "account deactivated", "账号停用", "账号禁用",
    )
    return not any(hint in text for hint in terminal) and any(
        hint in text for hint in _REGISTRATION_PROXY_ERROR_HINTS
    )


def _close_browser_session(session) -> None:
    try:
        session.session.close()
    except Exception:
        pass


def _prepare_protocol_registration_session(
    proxy: str | None,
    registration_country: str = "",
) -> BrowserSession:
    """Create and preflight a fresh route, rotating at most four times.

    The preflight does not send an email address or trigger OTP, so replacing
    the session here is safe.  Once registration enters an identity/business
    node, its existing stage logic remains authoritative and no proxy replay is
    attempted.
    """
    from core.registration_ip import (
        detect_http_registration_geo,
        normalize_registration_geo,
        registration_geo_matches,
    )

    last_exc: BaseException | None = None
    used: set[str] = set()
    for attempt in range(1, _REGISTRATION_PROXY_MAX_ATTEMPTS + 1):
        session = BrowserSession(proxy=proxy if proxy is not None else None)
        route = str(getattr(session, "proxy", "") or "")
        # A pool may randomly return the same route.  Give it a few cheap draws
        # before accepting a repeat when the configured pool is smaller than
        # the retry budget.
        if proxy is None and route in used:
            for _ in range(6):
                _close_browser_session(session)
                session = BrowserSession(proxy=None)
                route = str(getattr(session, "proxy", "") or "")
                if route not in used:
                    break
        used.add(route)
        try:
            network_preflight(session)
            # BrowserSession exposes exit_geo even when its locale probe is
            # disabled.  Keep lightweight third-party/test adapters that do not
            # expose this capability compatible with the historical contract.
            if hasattr(session, "exit_geo"):
                geo = normalize_registration_geo(getattr(session, "exit_geo", None) or {})
                # AUTO_BROWSER_LOCALE_FROM_IP only controls the browser profile.
                # Registration still measures its route before sending identity.
                if not geo["ip"] or (registration_country and not geo["country"]):
                    measured = detect_http_registration_geo(
                        session,
                        log_prefix="[注册][代理预检]",
                        require_country=bool(registration_country),
                    )
                    geo = {
                        "ip": measured.get("ip") or geo.get("ip") or "",
                        "country": measured.get("country") or geo.get("country") or "",
                    }
                route_ok, route_error = registration_geo_matches(geo, registration_country)
                if not route_ok:
                    raise RuntimeError(route_error)
                existing_exit_geo = getattr(session, "exit_geo", None)
                if isinstance(existing_exit_geo, dict):
                    existing_exit_geo.update(geo)
                else:
                    session.exit_geo = dict(geo)
                logger.info(
                    "[注册][代理预检] 第 %s/%s 次通过：IP=%s country=%s target=%s",
                    attempt,
                    _REGISTRATION_PROXY_MAX_ATTEMPTS,
                    geo.get("ip") or "?",
                    geo.get("country") or "?",
                    registration_country or "不限",
                )
            return session
        except Exception as exc:
            last_exc = exc
            _close_browser_session(session)
            route_validation_error = "代理出口" in str(exc)
            if (
                attempt >= _REGISTRATION_PROXY_MAX_ATTEMPTS
                or (not route_validation_error and not _is_registration_proxy_error(exc))
            ):
                raise
            logger.warning(
                "[注册][代理重试] 网络预检失败，切换代理重试：%s/%s error=%s: %s",
                attempt,
                _REGISTRATION_PROXY_MAX_ATTEMPTS,
                type(exc).__name__,
                str(exc)[:220],
            )
            time.sleep(0.5)
    raise last_exc or RuntimeError("注册网络预检失败")


def configure_logging(verbose: bool = False) -> None:
    """配置 CLI 日志：默认简洁，--verbose 时显示完整步骤细节。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in root.handlers:
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    if verbose:
        logging.getLogger("core").setLevel(logging.DEBUG)
        return

    logging.getLogger("core").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def _is_success(result: dict) -> bool:
    """判断单次注册结果是否成功，集中收敛批量统计规则。"""
    return isinstance(result, dict) and bool(result.get("success"))


def _finalize_registration_session(
    session: BrowserSession,
    continue_url: str,
    email: str,
    callback_referer: str = "https://auth.openai.com/about-you",
) -> tuple[dict, str]:
    """
    完成 OAuth 回调并拉取 accessToken。

    create_account 返回只代表创建接口通过，真正可用必须等 chatgpt.com
    写入登录态 cookie 且 /api/auth/session 返回 accessToken。
    """
    if not continue_url:
        raise RuntimeError("create_account 响应缺少 continue_url，无法完成 OAuth 回调")

    last_exc: Exception | None = None
    for attempt in range(1, _FINALIZE_SESSION_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                f"[登录态] 完成 OAuth 回调并拉取 Token：{email} "
                f"(尝试 {attempt}/{_FINALIZE_SESSION_MAX_ATTEMPTS})"
            )
            follow_oauth_callback(session, continue_url, referer=callback_referer)
            human_delay("post_auth")
            session_info = fetch_session(session)
            access_token = session_info.get("accessToken")
            if not access_token:
                raise RuntimeError("session 响应缺少 accessToken")
            logger.info(f"[登录态] 已拿到 accessToken：{email}")
            return session_info, access_token
        except Exception as exc:
            last_exc = exc
            if attempt >= _FINALIZE_SESSION_MAX_ATTEMPTS:
                break
            backoff = _FINALIZE_SESSION_BACKOFF_BASE ** (attempt - 1)
            logger.warning(
                f"[登录态] 回调或拉取 Token 失败：{email}，"
                f"{type(exc).__name__}: {str(exc)[:180]}，{backoff:.1f}s 后重试"
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"OAuth 回调/拉取 Token 重试耗尽：{email}，"
        f"最后错误：{type(last_exc).__name__ if last_exc else 'Unknown'}: {last_exc}"
    ) from last_exc


def generate_display_name() -> str:
    """生成只包含英文字母和空格的显示名，符合注册接口限制。"""
    return random_display_name()


def prepare_registration_inputs() -> tuple[str, str, str]:
    """按 CLI 规则准备一次注册所需的邮箱、显示名和生日。"""
    email = REGISTER_EMAIL
    name = REGISTER_NAME
    birthday = generate_random_birthday()

    # 邮箱：留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取
    if not email:
        if _email_cfg.USE_EMAIL_SERVICE:
            email = acquire_email()
            logger.debug(f"自动获取邮箱: {email}")
        else:
            email = input("请输入注册邮箱: ").strip()

    # 显示名称：未填则随机生成
    # OpenAI 限制：name_invalid_chars —— 只允许字母和空格，不能含数字/标点
    if not name:
        if _email_cfg.USE_EMAIL_SERVICE:
            name = generate_display_name()
            logger.debug(f"自动生成显示名称: {name}")
        else:
            name = input("请输入显示名称: ").strip()

    if not all([email, name]):
        raise RuntimeError("邮箱和名称不能为空")

    return email, name, birthday


def run_registration(
    email: str,
    name: str,
    birthday: str | None = None,
    proxy: str = None,
    otp_code: str = None,
    batch_dir=None,
    registration_country: str = "",
):
    """
    执行完整的 ChatGPT 注册流程。

    REGISTER_SET_PASSWORD=True 时与 Roxy 对齐：邮箱 → 密码注册 → 邮箱验证码
    → 姓名/生日 → AT → 2FA → 落库；关闭开关时保留原有 OTP-only 流程。

    Args:
        email: 注册邮箱
        name: 用户显示名称
        birthday: 生日，格式 YYYY-MM-DD
        proxy: 代理地址（不传则从 PROXY_POOL 随机抽）
        otp_code: 邮箱验证码（如果为None，会等待手动输入）
    """
    # 可选注册驱动：
    #   protocol     = 原有纯协议（curl_cffi）
    #   roxy         = RoxyBrowser 指纹浏览器 + Selenium
    #   cloak        = CloakBrowser + Playwright/Selenium 适配层
    #   browser_use  = Browser Use Cloud stealth Chromium + Playwright
    #   skyvern      = Skyvern Browser Sessions + Playwright
    driver_mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
    if driver_mode in ("roxy", "roxybrowser", "fingerprint", "browser"):
        from core.roxy_registration import run_roxy_registration
        return run_roxy_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            registration_country=registration_country,
        )
    if driver_mode in ("cloak", "cloakbrowser"):
        from core.cloakbrowser_registration import run_cloak_registration
        return run_cloak_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            registration_country=registration_country,
        )
    if driver_mode in ("browser_use", "browseruse", "browser-use", "bu"):
        from core.browser_use_registration import run_browser_use_registration
        return run_browser_use_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            registration_country=registration_country,
        )
    if driver_mode in ("skyvern", "sv"):
        from core.skyvern_registration import run_skyvern_registration
        return run_skyvern_registration(
            email=email,
            name=name,
            birthday=birthday or generate_random_birthday(),
            proxy=proxy,
            otp_code=otp_code,
            batch_dir=batch_dir,
            registration_country=registration_country,
        )
    if driver_mode not in ("protocol", "api", "http"):
        raise RuntimeError(
            f"不支持的 REGISTRATION_DRIVER={driver_mode!r}，可选 protocol / roxy / cloak / browser_use / skyvern"
        )

    # ============================================================
    # 协议注册：改用 gpt-outlook-register 移植的纯协议引擎（protocol_engine）。
    # 国家过滤 / 邮箱领取回收 / 账号存储 / 2FA / Codex / Flow 均在
    # core.protocol_runner 内衔接；异常在此收敛为失败 dict。
    # ============================================================
    from core.protocol_runner import run_protocol_registration
    try:
        return run_protocol_registration(
            email=email,
            name=name,
            birthday=birthday,
            proxy=proxy,
            registration_country=registration_country,
        )
    except Exception as e:
        logger.error(f"[失败] {email}: {type(e).__name__}: {e}")
        return {"success": False, "email": email, "error": str(e)}


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="ChatGPT 协议注册 CLI")
    parser.add_argument("-n", "--count", type=int, default=1, help="连续注册数量，默认 1")
    parser.add_argument("--workers", type=int, default=1, help="并发注册线程数，默认 1（串行）")
    parser.add_argument("--country", default="", help="目标注册国家，两位国家码或常见国家名称；留空不限")
    parser.add_argument("--delay", type=float, default=0, help="并发模式(--workers>1)：任务之间的启动错峰间隔秒数；串行模式：每次注册结束后的间隔秒数")
    parser.add_argument("--continue-on-fail", action="store_true", help="单个账号失败后继续注册下一个")
    parser.add_argument("--verbose", action="store_true", help="显示详细步骤日志和错误堆栈")
    args = parser.parse_args()
    configure_logging(args.verbose)

    if args.count < 1:
        logger.error("注册数量必须大于 0")
        sys.exit(1)

    if args.workers < 1:
        logger.error("并发线程数必须大于 0")
        sys.exit(1)

    if args.count > 1 and REGISTER_EMAIL:
        logger.error("config.REGISTER_EMAIL 已固定邮箱，不适合批量注册；请留空后再使用 --count")
        sys.exit(1)

    if args.workers > 1 and not _email_cfg.USE_EMAIL_SERVICE:
        logger.error("多线程注册需要启用 Outlook 自动取件；请开启 USE_EMAIL_SERVICE 或改用 --workers 1")
        sys.exit(1)

    if args.workers > args.count:
        logger.info(f"[批量] 并发线程数 {args.workers} 大于目标数量，已按 {args.count} 个任务执行")
        args.workers = args.count

    if args.workers > 1:
        batch_dir = create_batch_archive_dir(args.count, args.workers)
        logger.info(f"[批量] 本批次归档目录：{batch_dir}")
        results = run_parallel_batch(
            args.count,
            args.workers,
            args.delay,
            args.continue_on_fail,
            batch_dir,
            args.country,
        )
    else:
        batch_dir = create_batch_archive_dir(args.count, args.workers)
        logger.info(f"[批量] 本批次归档目录：{batch_dir}")
        results = run_serial_batch(
            args.count,
            args.delay,
            args.continue_on_fail,
            batch_dir,
            args.country,
        )

    success_count = sum(1 for r in results if _is_success(r))
    flow_success_count = sum(
        1 for r in results
        if _is_success(r) and isinstance(r.get("flow"), dict) and r["flow"].get("ok")
    )
    flow_failed_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("flow"), dict)
        and r["flow"].get("status") == "failed"
    )
    flow_skipped_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("flow"), dict)
        and r["flow"].get("status") == "skipped"
    )
    codex_success_count = sum(
        1 for r in results
        if _is_success(r) and isinstance(r.get("codex"), dict) and r["codex"].get("ok")
    )
    codex_failed_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("codex"), dict)
        and r["codex"].get("status") == "failed"
    )
    codex_skipped_count = sum(
        1 for r in results
        if _is_success(r)
        and isinstance(r.get("codex"), dict)
        and r["codex"].get("status") == "skipped"
    )
    logger.info(f"[批量] 完成：成功 {success_count} / 尝试 {len(results)} / 目标 {args.count}")
    if success_count:
        logger.info(
            f"[批量] Flow：成功 {flow_success_count} / 失败 {flow_failed_count} / 跳过 {flow_skipped_count}"
        )
        logger.info(
            f"[批量] Codex：成功 {codex_success_count} / 失败 {codex_failed_count} / 跳过 {codex_skipped_count}"
        )
    sys.exit(0 if success_count == args.count else 1)


def run_one_batch_item(index: int, total: int, batch_dir=None, registration_country: str = "") -> dict:
    """执行批量注册中的一个任务，返回结构化结果。"""
    logger.info(f"[批量] 开始第 {index + 1}/{total} 个注册")
    try:
        email, name, birthday = prepare_registration_inputs()
        return run_registration(
            email=email,
            name=name,
            birthday=birthday,
            batch_dir=batch_dir,
            registration_country=registration_country,
            # proxy 不传 → BrowserSession 会从 PROXY_POOL 随机抽
        )
    except Exception as exc:
        logger.error(f"[批量] 第 {index + 1} 个注册准备阶段失败: {type(exc).__name__}: {exc}")
        logger.debug("准备阶段错误详情:", exc_info=True)
        return {"success": False, "error": str(exc)}


def run_serial_batch(
    count: int,
    delay: float,
    continue_on_fail: bool,
    batch_dir=None,
    registration_country: str = "",
) -> list[dict]:
    """按原有串行方式执行批量注册。"""
    results = []
    for index in range(count):
        result = run_one_batch_item(index, count, batch_dir, registration_country)
        results.append(result)
        if not _is_success(result) and not continue_on_fail:
            logger.error("[批量] 当前账号失败，已停止。需要继续跑可加 --continue-on-fail")
            break

        if delay > 0 and index < count - 1:
            logger.info(f"[批量] 等待 {delay} 秒后继续")
            time.sleep(delay)
    return results


def run_parallel_batch(
    count: int,
    workers: int,
    delay: float,
    continue_on_fail: bool,
    batch_dir=None,
    registration_country: str = "",
) -> list[dict]:
    """使用线程池并发执行批量注册。"""
    logger.info(f"[批量] 启用多线程注册：目标 {count}，并发 {workers}")
    if delay > 0:
        logger.info(f"[批量] 并发模式下 --delay={delay} 表示提交任务之间的错峰间隔")

    results: list[dict] = []
    future_to_index = {}
    next_index = 0
    stop_submitting = False

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal next_index
        if stop_submitting or next_index >= count:
            return False
        future = executor.submit(
            run_one_batch_item,
            next_index,
            count,
            batch_dir,
            registration_country,
        )
        future_to_index[future] = next_index
        next_index += 1
        if delay > 0 and next_index < count:
            time.sleep(delay)
        return True

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reg-cli") as executor:
        while len(future_to_index) < workers and submit_next(executor):
            pass

        while future_to_index:
            done, _ = wait(future_to_index, return_when=FIRST_COMPLETED)
            for future in done:
                index = future_to_index.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"[批量] 第 {index + 1}/{count} 个注册线程异常: {type(exc).__name__}: {exc}")
                    logger.debug("线程错误详情:", exc_info=True)
                    result = {"success": False, "error": str(exc)}
                results.append(result)

                if not _is_success(result) and not continue_on_fail:
                    stop_submitting = True
                    logger.error("[批量] 当前账号失败，已停止提交新任务。已开始的任务会继续跑完。")

            while len(future_to_index) < workers and submit_next(executor):
                pass

    return results


if __name__ == "__main__":
    main()
