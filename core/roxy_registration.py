# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 指纹浏览器 + Selenium 执行 ChatGPT 注册。"""
from __future__ import annotations

import json
import logging
import random
import string
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from config import roxybrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay
from core.registration_ip import (
    detect_selenium_registration_geo,
    detect_selenium_registration_ip,
    normalize_country_code,
    registration_geo_matches,
)
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult

logger = logging.getLogger(__name__)


def _log_prefix(driver=None) -> str:
    """按当前浏览器实现返回注册日志前缀。

    CloakBrowser 复用 Roxy 的页面操作函数；这些共享函数必须跟随实际 driver
    输出 `[Cloak注册]`，避免 Cloak 流程里混入 `[Roxy注册]` 日志。
    """
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
        if driver is not None and driver.__class__.__name__ == "CloakSeleniumDriver":
            return "[Cloak注册]"
    except Exception:
        pass
    return "[Roxy注册]"


# 状态机轮询循环里，检测到 Cloudflare 挑战时额外给的一次完整处理窗口（秒）。
# 只延长一次，避免验证卡住时无限等待。
_CF_SOLVE_WINDOW = 30
_CF_TRANSITION_TIMEOUT = 30
# 密码提交后的页面转场可能包含一次完整的 CF/授权跳转。只在超过这个窗口后，
# 仍能确认页面没有离开密码页时才判定失败，避免过早进入 OTP 阶段。
_PASSWORD_TRANSITION_TIMEOUT = max(_CF_TRANSITION_TIMEOUT, 30)

_TRANSIENT_NAVIGATION_ERRORS = (
    "execution context was destroyed",
    "context was destroyed",
    "cannot find context",
    "because of a navigation",
    "frame was detached",
    "targetclosederror",
    "target closed",
    "target page, context or browser has been closed",
    "page has been closed",
)


def _is_transient_navigation_error(exc: BaseException, driver=None) -> bool:
    """Return whether a browser error can occur while a page is navigating."""
    message = f"{type(exc).__name__}: {exc}".lower()
    if not any(hint in message for hint in _TRANSIENT_NAVIGATION_ERRORS):
        return False

    page = getattr(driver, "page", None)
    is_closed = getattr(page, "is_closed", None)
    if callable(is_closed):
        try:
            closed = is_closed()
            if isinstance(closed, bool) and closed:
                return False
        except Exception:
            pass
    return True


def _probe_state_unsettled(state: dict | None) -> bool:
    """A failed DOM probe is an unknown transition state, never a negative."""
    return bool(isinstance(state, dict) and state.get("error"))


def _cf_active(driver) -> bool:
    """当前页面是否存在需要处理的 Cloudflare/Turnstile 挑战。"""
    try:
        from core.cloudflare_verification import challenge_kind
        return challenge_kind(driver) != "none"
    except Exception as exc:
        logger.debug(
            "%s[CF] challenge probe unsettled; keep waiting: %s: %s",
            _log_prefix(driver),
            type(exc).__name__,
            str(exc)[:160],
        )
        return True


def _solve_cf(driver, *, timeout=None, label: str = "") -> bool:
    """自动处理当前页面上的 Cloudflare/Turnstile 挑战；无挑战时快速返回 True。

    timeout=None 时使用 config.browser.CLOUD_FLARE_SOLVE_TIMEOUT。
    处理失败（升级为人工验证）不会抛异常，返回 False 让上层自行决定。
    """
    try:
        from config import browser as _browser_cfg
        if not bool(getattr(_browser_cfg, "CLOUD_FLARE_AUTO_SOLVE", True)):
            return True
        from core.cloudflare_verification import solve_cloudflare_challenge
        return solve_cloudflare_challenge(
            driver,
            timeout=timeout,
            label=label,
            log_prefix=_log_prefix(driver),
        )
    except Exception as exc:
        logger.warning("%s[CF] 处理 Cloudflare 挑战异常，保留未解决状态：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:160])
        return False


def _cf_watch_tick(driver, *, end: float, window: int = _CF_SOLVE_WINDOW, label: str = "", done: bool = False):
    """轮询循环内的 CF 挑战处理。

    检测到挑战时返回 (True, 是否首次处理, 可能已延长的 end)：调用方应 continue，
    跳过常规状态检测（挑战页上状态检测会误判）。首次检测时用 window 秒窗口
    自动尝试解决，并给循环延长一次截止时间。
    """
    if not _cf_active(driver):
        return False, done, end
    if not done:
        new_end = max(end, time.time() + int(window))
        _solve_cf(driver, timeout=int(window), label=label)
        return True, True, new_end
    return True, done, end


def _build_driver(opened: RoxyOpenResult):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

    if opened.debugger_address:
        logger.info("[Roxy] Selenium 连接 debuggerAddress=%s", opened.debugger_address)
        options = Options()
        # 页面里长轮询/风控脚本偶尔会让 driver.get 等到超时；eager 只等 DOMContentLoaded。
        options.page_load_strategy = "eager"
        options.add_experimental_option("debuggerAddress", opened.debugger_address)
        driver_path = ""
        try:
            raw_data = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
            if isinstance(raw_data, dict):
                driver_path = str(raw_data.get("driver") or raw_data.get("driverPath") or raw_data.get("driver_path") or "").strip()
        except Exception:
            driver_path = ""
        if driver_path:
            logger.info("[Roxy] 使用 Roxy chromedriver=%s", driver_path)
            driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
        else:
            driver = webdriver.Chrome(options=options)
        _apply_browser_automation_mask(driver)
        return driver

    if opened.webdriver_url:
        logger.info("[Roxy] Selenium 连接 webdriver_url=%s", opened.webdriver_url)
        options = Options()
        options.page_load_strategy = "eager"
        driver = RemoteWebDriver(command_executor=opened.webdriver_url, options=options)
        _apply_browser_automation_mask(driver)
        return driver

    raise RuntimeError("Roxy 未返回可连接的 Selenium 地址")


def _center_browser_window(driver) -> None:
    """把可见的 Roxy 窗口移动到 Windows 主屏工作区中央。"""
    if bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False)):
        return
    try:
        import platform
        if platform.system().lower() != "windows":
            return
        import ctypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        work_area = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            raise OSError("无法读取 Windows 工作区")
        size = driver.get_window_size()
        width = max(1, int(size.get("width") or 1))
        height = max(1, int(size.get("height") or 1))
        x = int(work_area.left + max(0, (work_area.right - work_area.left - width) // 2))
        y = int(work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2))
        driver.set_window_position(x, y)
        logger.info("[Roxy] 浏览器窗口已居中：x=%s y=%s width=%s height=%s", x, y, width, height)
    except Exception as exc:
        logger.warning("[Roxy] 浏览器窗口居中失败，继续执行：%s", exc)


def _wait(driver, timeout: int | None = None):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver, timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))


def _fast_enabled() -> bool:
    """FAST 模式总开关。"""
    try:
        from config import fast_mode as _fast
        return bool(getattr(_fast, "FAST_MODE_ENABLED", False))
    except Exception:
        return False


def _fast_sleep_factor() -> float:
    try:
        from config import fast_mode as _fast
        return float(getattr(_fast, "FAST_MODE_SLEEP_FACTOR", 1.0) or 1.0)
    except Exception:
        return 1.0


def _fsleep(seconds: float) -> None:
    """FAST 模式下按系数压缩固定 sleep；普通模式原样等待。

    网页真正加载、CF 挑战、OTP 邮件到达等硬等待不在这条路径上（那些在
    _safe_get / _solve_cf / wait_for_otp 里），这里只压缩过渡与轮询步进。
    """
    if seconds <= 0:
        return
    if _fast_enabled():
        seconds = max(0.03, seconds * _fast_sleep_factor())
    import time as _t
    _t.sleep(seconds)


def _safe_get(driver, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = (), script_timeout: int | None = None) -> None:
    """带容错的页面跳转。

    Roxy/Chrome 150 偶发 `Timed out receiving message from renderer`，实际页面可能已经可用。
    这里超时后先 `window.stop()`，只要当前 URL/DOM 已进入目标页就继续；否则重试一次。

    script_timeout: 跳转后 execute_async_script 的脚本执行超时（秒）。
        默认 None 时沿用原行为 8s（不影响其它调用方）；2FA 等浏览器内 fetch 长链路
        必须显式传更大值，否则每次跳转都会把 script timeout 压回 8s，覆盖上层放宽。
    """
    from selenium.common.exceptions import TimeoutException, WebDriverException

    last_exc: Exception | None = None
    old_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    script_timeout = 8 if script_timeout is None else int(script_timeout)
    hosts = tuple(h.lower() for h in (accept_hosts or ()))
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                driver.set_page_load_timeout(max(10, int(timeout)))
                driver.set_script_timeout(script_timeout)
            except Exception:
                pass
            driver.get(url)
            return
        except TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "%s 页面加载超时，尝试停止加载后检查 DOM：url=%s attempt=%s/%s error=%s",
                _log_prefix(driver), url, attempt, attempts, str(exc).splitlines()[0] if str(exc) else "TimeoutException",
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            _fsleep(1.0)
            try:
                current = str(driver.current_url or "").lower()
            except Exception:
                current = ""
            try:
                ready = str(driver.execute_script("return document.readyState || ''") or "")
                has_body = bool(driver.execute_script("return !!document.body"))
            except Exception:
                ready = ""
                has_body = False
            target_ok = any(h in current for h in hosts) if hosts else (url.split("/", 3)[2].lower() in current)
            if target_ok and has_body:
                logger.info(
                    "%s 页面加载虽超时但 DOM 可用，继续流程：current=%s readyState=%s",
                    _log_prefix(driver), current[:180], ready or "-",
                )
                return
            if attempt < attempts:
                try:
                    driver.get("about:blank")
                except Exception:
                    pass
                _fsleep(1.5 * attempt)
                continue
        except WebDriverException as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning("%s 页面跳转失败，准备重试：url=%s attempt=%s/%s error=%s", _log_prefix(driver), url, attempt, attempts, exc)
                _fsleep(1.5 * attempt)
                continue
            raise
        finally:
            try:
                driver.set_page_load_timeout(old_timeout)
            except Exception:
                pass
    raise last_exc or RuntimeError(f"页面跳转失败: {url}")


def _visible(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:
        return False


def _browser_actions_enabled() -> bool:
    try:
        from config import humanize as _hcfg
        return bool(getattr(_hcfg, "ENABLE_HUMANIZE_BROWSER_ACTIONS", True))
    except Exception:
        return True


def _apply_browser_automation_mask(driver) -> None:
    """连接 Selenium 后尽量降低明显自动化特征；失败不影响主流程。

    仅用于 Roxy 的 Selenium 路径。Cloak 使用编译进 Chromium 的原生补丁，
    额外注入该脚本会制造重复、可观察的属性覆盖。
    """
    if not _browser_actions_enabled():
        return
    try:
        from core.stealth_mask import inject_automation_mask
        inject_automation_mask(driver)
        logger.info("%s 已注入浏览器自动化特征弱化脚本", _log_prefix(driver))
    except Exception as exc:
        logger.debug("%s 注入自动化特征弱化脚本失败：%s", _log_prefix(driver), exc)


def _human_scroll_to(driver, el) -> None:
    try:
        block = random.choice(["center", "nearest", "center"])
        driver.execute_script("arguments[0].scrollIntoView({block: arguments[1], inline:'nearest'});", el, block)
        if _browser_actions_enabled():
            _fsleep(random.uniform(0.08, 0.35))
            # 轻微滚动抖动，避免每次都精准居中。
            driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(-90, 90))
            _fsleep(random.uniform(0.05, 0.22))
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", el)
    except Exception:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass


def _human_click(driver, el, *, label: str = "") -> None:
    """快速人工化点击。

    之前用 ActionChains 在 Roxy/Chrome 150 上偶发卡住 1-2 分钟，导致邮箱提交很慢。
    这里改为 CDP 派发鼠标事件；没有 CDP 时再用 JS/原生 click 兜底。
    """
    _human_scroll_to(driver, el)
    if not _browser_actions_enabled():
        _fsleep(0.2)
        el.click()
        return
    try:
        human_delay("click")
        point = driver.execute_script(r"""
        const el = arguments[0];
        const r = el.getBoundingClientRect();
        const x = r.left + r.width * (0.30 + Math.random() * 0.40);
        const y = r.top + r.height * (0.35 + Math.random() * 0.30);
        return {x, y, w:r.width, h:r.height};
        """, el) or {}
        x = float(point.get("x") or 0)
        y = float(point.get("y") or 0)
        if hasattr(driver, "execute_cdp_cmd") and x > 0 and y > 0:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            _fsleep(random.uniform(0.05, 0.22))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            _fsleep(random.uniform(0.035, 0.13))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        else:
            driver.execute_script(r"""
            const el = arguments[0];
            el.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            el.click();
            """, el)
    except Exception as exc:
        logger.debug("%s 人工化点击失败，回退 el.click label=%s err=%s", _log_prefix(driver), label, exc)
        _fsleep(random.uniform(0.12, 0.45))
        try:
            driver.execute_script("arguments[0].click();", el)
        except Exception:
            el.click()


def _human_type_text(driver, el, value: str, *, clear: bool = True) -> None:
    """按字符/小段输入，触发真实 key events；失败时回退 JS setter。"""
    if not _browser_actions_enabled():
        if clear:
            try:
                el.clear()
            except Exception:
                pass
        el.send_keys(value)
        return
    try:
        _human_scroll_to(driver, el)
        try:
            _human_click(driver, el, label="input_focus")
        except Exception:
            driver.execute_script("arguments[0].focus();", el)
        if clear:
            from selenium.webdriver.common.keys import Keys
            mod = Keys.COMMAND
            try:
                import platform
                if platform.system().lower() != "darwin":
                    mod = Keys.CONTROL
            except Exception:
                pass
            try:
                el.send_keys(mod, "a")
                _fsleep(random.uniform(0.04, 0.16))
                el.send_keys(Keys.BACKSPACE)
            except Exception:
                try:
                    el.clear()
                except Exception:
                    pass
        text = str(value)
        i = 0
        while i < len(text):
            # 邮箱/密码整体仍逐字符，但偶尔 2 字符一组，节奏更自然。
            step = 2 if random.random() < 0.12 and i + 1 < len(text) else 1
            el.send_keys(text[i:i + step])
            i += step
            human_delay("keystroke")
            if i < len(text) and random.random() < 0.08:
                human_delay("typing_pause")
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            el,
        )
    except Exception as exc:
        logger.debug("%s 人工化输入失败，回退 JS setter err=%s", _log_prefix(driver), exc)
        _set_element_value(driver, el, value)


def _page_warmup(driver, *, reason: str = "") -> None:
    if not _browser_actions_enabled():
        return
    try:
        human_delay("page_warmup")
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(80, 360),
                "y": random.randint(80, 260),
            })
    except Exception:
        pass


def _find_any(driver, selectors: list[str], timeout: int | None = None):
    from selenium.webdriver.common.by import By

    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last = None
    while time.time() < end:
        for selector in selectors:
            try:
                by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
                items = driver.find_elements(by, selector)
                for item in items:
                    if _visible(item):
                        return item
            except Exception as exc:
                last = exc
        _fsleep(0.4)
    raise RuntimeError(f"找不到页面元素: {selectors}; last={last}")


def _click_any(driver, selectors: list[str], timeout: int | None = None) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_click(driver, el, label="click_any")


def _type_any(driver, selectors: list[str], value: str, timeout: int | None = None, clear: bool = True) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_type_text(driver, el, value, clear=clear)


_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input#email-input",
    "input[autocomplete='email']",
]


def _email_entry_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const attrText = el => [
          el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
          el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
          el.getAttribute('data-auth-provider'), el.getAttribute('href'), el.getAttribute('action'),
          el.getAttribute('formaction'), el.getAttribute('value')
        ].filter(Boolean).join(' ').toLowerCase();
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''
        })).slice(0, 30);
        const actions = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({tag: el.tagName, type: el.getAttribute('type') || '', attrs: attrText(el)})).slice(0, 40);
        return {url: location.href, title: document.title, inputs, actions};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _find_visible_email_input_js(driver):
    return driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const selectors = [
      'input[type="email"]',
      'input[name="email"]',
      'input[name="username"]',
      'input#email-input',
      'input[autocomplete="email"]'
    ];
    for (const sel of selectors) {
      const el = [...document.querySelectorAll(sel)].find(visible);
      if (el) return el;
    }
    return null;
    """)


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。"""
    try:
        return bool(driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        if (/oauth|authorize|consent/.test(url) && !/login|signup|identifier|email-verification/.test(url)) return true;
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        if (formsWithEmail) return false;
        const actions = [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"]')]
          .map(el => [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('href'),
            el.getAttribute('formaction'), el.value, el.className].filter(Boolean).join(' ').toLowerCase())
          .join(' ');
        return /oauth|authorize|consent|grant|allow/.test(actions) && !/email|username/.test(actions);
        """))
    except Exception:
        return False


def _is_external_idp_url(url: str) -> bool:
    u = str(url or '').lower()
    return any(x in u for x in (
        'accounts.google.', 'google.com/o/oauth', 'appleid.apple.', 'login.microsoftonline.',
        'login.live.', 'github.com/login/oauth', 'facebook.com/', 'saml', 'sso'
    ))


def _assert_not_external_idp(driver, label: str = '') -> None:
    try:
        current = str(driver.current_url or '')
    except Exception:
        current = ''
    if _is_external_idp_url(current):
        raise RuntimeError(f"误入第三方账号授权页（{label}）：{current}")


def _click_email_entry_option(driver) -> bool:
    """点击“邮箱方式”入口；只看 DOM 技术属性，不看按钮可见文案，并显式排除 Google 等第三方。"""
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，跳过邮箱入口兜底点击", _log_prefix(driver))
        return False
    target = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const attrText = el => {
      const own = [
        el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
        el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
        el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'), el.getAttribute('href'), el.getAttribute('action'),
        el.getAttribute('formaction'), el.getAttribute('value'), el.getAttribute('aria-label'), el.className
      ].filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' ')).join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
    const good = /(^|[^a-z])(email|mail|username|passwordless|otp|magic)([^a-z]|$)/;
    const candidates = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
      .filter(visible)
      .map(el => ({el, attrs: attrText(el), hasLogo: !!el.querySelector('img,svg,use')}))
      .filter(x => good.test(x.attrs) && !bad.test(x.attrs) && !x.hasLogo);
    if (candidates.length !== 1) return null;
    candidates[0].el.scrollIntoView({block:'center'});
    return candidates[0].el;
    """)
    if target:
        _human_click(driver, target, label="email_entry")
        return True
    return False


def _type_email_address(driver, email: str, timeout: int | None = None) -> None:
    """进入邮箱登录/注册方式并填写邮箱。全程不依赖页面可见文字，避免非日本出口本地化后误点 Google。"""
    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last_state = None
    clicked_email_option = False
    while time.time() < end:
        try:
            el = _find_visible_email_input_js(driver)
            if el:
                _human_type_text(driver, el, email, clear=True)
                return
            last_state = _email_entry_state(driver)
            if _probe_state_unsettled(last_state):
                _fsleep(0.4)
                continue
            if not clicked_email_option and _click_email_entry_option(driver):
                clicked_email_option = True
                _fsleep(1.0)
                _assert_not_external_idp(driver, "点击邮箱入口后")
                continue
        except Exception as exc:
            if not _is_transient_navigation_error(exc, driver):
                raise
            last_state = {"error": f"{type(exc).__name__}: {exc}"}
            logger.debug("%s 邮箱页正在导航，延后重试输入框探测：%s", _log_prefix(driver), str(exc)[:160])
        _fsleep(0.4)
    raise RuntimeError(f"找不到邮箱输入框/邮箱入口（未使用文字识别），state={last_state}")


def _submit_nearest_form_for_active_input(driver) -> bool:
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，禁止执行邮箱提交", _log_prefix(driver))
        return False
    result = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_email_input'};
    const value = String(input.value || '').trim();
    if (!value || !value.includes('@')) return {ok:false, reason:'email_value_not_ready', value};
    const form = input.closest('form');
    if (!form) return {ok:false, reason:'missing_form'};

    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|sso|saml|idp|provider|authorize|consent|grant|allow/;
    const attrText = el => {
      const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
        el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
        el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
        .filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' '))
        .join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const inputRect = input.getBoundingClientRect();
    const formId = form.getAttribute('id') || '';
    const scopedButtons = [
      ...form.querySelectorAll('button,input[type="submit"]'),
      ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
    ].filter((el, idx, arr) => arr.indexOf(el) === idx);
    const rawButtons = scopedButtons
      .filter(visible)
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const attrs = attrText(el);
        const hasLogo = !!el.querySelector('img,svg,use');
        const isBad = bad.test(attrs) || hasLogo;
        const belowInput = r.top >= inputRect.bottom - 10;
        const distance = Math.max(0, r.top - inputRect.bottom) + Math.abs((r.left + r.right) / 2 - (inputRect.left + inputRect.right) / 2) / 10;
        const cls = String(el.className || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        // ChatGPT 新版邮箱页的主按钮形如：
        // <button class="... btn-primary ... w-full ..." type="submit"><div>続行</div></button>
        // 优先选择同 form 下的 primary submit，而不是因为多个按钮距离接近误判歧义。
        const isPrimarySubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && type === 'submit'
          && (/\bbtn-primary\b/.test(cls) || /\b_primary_/.test(cls) || /\bw-full\b/.test(cls));
        const score = (isPrimarySubmit ? 1000 : 0) + (type === 'submit' ? 100 : 0) - distance;
        return {el, idx, attrs, isBad, hasLogo, belowInput, distance, score, isPrimarySubmit, tag: el.tagName, type};
      });
    const safe = rawButtons.filter(x => !x.isBad && x.belowInput)
      .sort((a,b) => b.score - a.score || a.distance - b.distance || a.idx - b.idx);
    if (!safe.length) {
      return {ok:false, reason:'no_safe_submit', buttons: rawButtons.map(x => ({idx:x.idx, isBad:x.isBad, hasLogo:x.hasLogo, belowInput:x.belowInput, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    // 多个安全按钮时，若没有明确 primary submit，且距离接近，才认为页面歧义。
    if (!safe[0].isPrimarySubmit && safe.length > 1 && Math.abs(safe[0].distance - safe[1].distance) < 8) {
      return {ok:false, reason:'ambiguous_submit', buttons: safe.slice(0,3).map(x => ({idx:x.idx, distance:x.distance, score:x.score, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    const target = safe[0].el;
    target.scrollIntoView({block:'center'});
    window.__roxy_email_submit_debug = {at: Date.now(), targetAttrs: safe[0].attrs.slice(0,240), buttonCount: rawButtons.length, primary:safe[0].isPrimarySubmit};
    return {ok:true, reason:safe[0].isPrimarySubmit ? 'primary_submit' : 'safe_submit', target, targetAttrs:safe[0].attrs.slice(0,160), primary:safe[0].isPrimarySubmit};
    """) or {}
    if result.get("ok"):
        target = result.get("target")
        if target:
            _human_click(driver, target, label="email_submit")
        else:
            logger.warning("%s 邮箱提交未返回目标元素，回退 requestSubmit", _log_prefix(driver))
            driver.execute_script("document.querySelector('form')?.requestSubmit?.();")
        logger.info("%s 邮箱表单安全提交：%s", _log_prefix(driver), result)
        _fsleep(0.8)
        _assert_not_external_idp(driver, "提交邮箱后")
        return True
    logger.warning("%s 未执行邮箱提交：%s", _log_prefix(driver), result)
    return False


def _current_email_input_value(driver) -> str:
    try:
        state = _email_input_value_state(driver)
        for item in state.get("inputs") or []:
            value = str(item.get("value") or "").strip()
            if "@" in value:
                return value
    except Exception:
        pass
    return ""


def _stabilize_email_input_before_submit(driver, email: str) -> dict:
    """提交前把 DOM value / React 受控状态 / blur-change 状态统一稳定下来。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;

        // 让 React/表单校验尽量收到完整输入链路。
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        return {
          ok:true,
          value: input.value,
          active: document.activeElement === input,
          hasForm: !!form,
          hasSubmit: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : null,
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_form_stable(driver, email: str) -> dict:
    """第一次提交就按“补交成功”的方式执行：稳定 value 后 Enter + DOM click。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const editable = el => visible(el) && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(editable);
        if (!input) return {ok:false, reason:'missing_email_input'};
        if (!email || !email.includes('@')) return {ok:false, reason:'empty_email', value: email};

        const form = input.closest('form');
        if (!form) return {ok:false, reason:'missing_form'};

        const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
        const attrText = el => {
          const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
            el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
            .filter(Boolean).join(' ');
          const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
            .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
              x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
              .filter(Boolean).join(' '))
            .join(' ');
          return `${own} ${desc}`.toLowerCase();
        };

        const formId = form.getAttribute('id') || '';
        const buttons = [
          ...form.querySelectorAll('button,input[type="submit"]'),
          ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
        ].filter((el, idx, arr) => arr.indexOf(el) === idx)
          .filter(el => visible(el) && !bad.test(attrText(el)) && !el.querySelector('img,svg,use'));
        const submit = buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0] || null;
        if (!submit) return {ok:false, reason:'missing_safe_submit'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        submit.scrollIntoView({block:'center', inline:'nearest'});

        // 不要在 execute_script 同步提交：ChromeDriver 会等待前端导航，
        // Roxy/Chrome 150 上可能卡到 page/script timeout。每轮只触发一次
        // requestSubmit，避免 Enter 与 click 被 React 分别处理成两次提交。
        setTimeout(() => {
          try {
            input.focus();
            if (form && typeof form.requestSubmit === 'function') form.requestSubmit(submit);
            else if (submit && !submit.disabled) submit.click();
          } catch (_) {}
        }, 80);

        window.__roxy_email_submit_debug = {
          at: Date.now(),
          mode: 'stable_async_request_submit',
          value: input.value,
          submitAttrs: attrText(submit).slice(0, 240)
        };
        return {
          ok:true,
          reason:'stable_async_request_submit',
          value: input.value,
          submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          submitAttrs: attrText(submit).slice(0, 180),
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_step(driver, email: str | None = None) -> None:
    # 不再优先走浏览器内 NextAuth fetch：
    # Roxy/Chrome 150 下 execute_async_script + fetch 偶发卡到 script timeout；
    # 实测 UI 首次提交后若停在 /auth/login?email=...，由 _recover_email_submit_if_stuck 补交表单更稳定。
    email_value = str(email or _current_email_input_value(driver) or "").strip()
    stable = _stabilize_email_input_before_submit(driver, email_value)
    logger.info("%s 邮箱提交前状态稳定：%s", _log_prefix(driver), stable)
    _fsleep(random.uniform(0.8, 1.8) if _browser_actions_enabled() else 0.4)

    stable_submit = _submit_email_form_stable(driver, email_value)
    if stable_submit.get("ok"):
        logger.info("%s 邮箱稳定表单提交：%s", _log_prefix(driver), stable_submit)
        _fsleep(1.0)
        _assert_not_external_idp(driver, "稳定表单提交邮箱后")
        return
    logger.warning("%s 邮箱稳定表单提交失败，回退 UI 点击提交：%s", _log_prefix(driver), stable_submit)
    if _submit_nearest_form_for_active_input(driver):
        return
    raise RuntimeError(f"无法提交邮箱步骤（拒绝按页面文字或首个 submit 兜底，避免误点第三方登录），state={_email_entry_state(driver)}")


def _recover_email_submit_if_stuck(driver, email: str) -> dict:
    """邮箱提交后停在 /auth/login?email= 且输入框被清空时，补一次原生表单提交。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        setTimeout(() => {
          try {
            if (form && typeof form.requestSubmit === 'function') form.requestSubmit(submit);
            else if (submit && !submit.disabled) submit.click();
          } catch (_) {}
        }, 80);
        return {ok:true, reason:'resubmitted_email_form', value: input.value, hasForm: !!form, hasSubmit: !!submit};
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_via_browser_nextauth(driver, email: str) -> dict:
    """在 Roxy 浏览器上下文里调用 ChatGPT NextAuth signin。

    UI submit 在 Roxy/Chrome 150 上会偶发只跳到 `/auth/login?email=...` 后停住。
    这里改走浏览器页面内 fetch，仍使用当前 Roxy 浏览器的 cookie / 指纹环境，
    拿到 auth.openai.com authorize URL 后先返回 Python，再由 Selenium 导航。
    这样不会因 JS callback 前页面被 location.assign 销毁而卡到 script timeout。
    """
    try:
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" not in current:
            return {"ok": False, "reason": "not_on_chatgpt", "url": current[:180]}
    except Exception:
        current = ""

    did = str(uuid.uuid4())
    auth_log_id = str(uuid.uuid4())
    old_script_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    try:
        try:
            driver.set_script_timeout(25)
        except Exception:
            pass
        result = driver.execute_async_script(r"""
        const email = String(arguments[0] || '').trim();
        const did = String(arguments[1] || '');
        const authLogId = String(arguments[2] || '');
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const csrfResp = await fetch('/api/auth/csrf', {
              method: 'GET',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              }
            });
            const csrfText = await csrfResp.text();
            let csrfData = {};
            try { csrfData = JSON.parse(csrfText); } catch (_) {}
            const csrfToken = csrfData.csrfToken || '';
            if (!csrfResp.ok || !csrfToken) {
              done({ok:false, stage:'csrf', status:csrfResp.status, body:csrfText.slice(0, 500)});
              return;
            }

            const q = new URLSearchParams({
              prompt: 'login',
              'ext-oai-did': did,
              auth_session_logging_id: authLogId,
              'ext-passkey-client-capabilities': '11111',
              screen_hint: 'login_or_signup',
              login_hint: email
            });
            const body = new URLSearchParams({
              callbackUrl: 'https://chatgpt.com/',
              csrfToken,
              json: 'true'
            });
            const resp = await fetch('/api/auth/signin/openai?' + q.toString(), {
              method: 'POST',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              },
              body: body.toString()
            });
            const text = await resp.text();
            let data = {};
            try { data = JSON.parse(text); } catch (_) {}
            let url = data.url || '';
            if (!resp.ok || !url) {
              done({ok:false, stage:'signin', status:resp.status, body:text.slice(0, 700)});
              return;
            }

            try {
              const u = new URL(url, location.href);
              if (!u.searchParams.get('screen_hint')) u.searchParams.set('screen_hint', 'login_or_signup');
              if (!u.searchParams.get('login_hint')) u.searchParams.set('login_hint', email);
              if (!u.searchParams.get('ext-oai-did')) u.searchParams.set('ext-oai-did', did);
              if (!u.searchParams.get('auth_session_logging_id')) u.searchParams.set('auth_session_logging_id', authLogId);
              url = u.toString();
            } catch (_) {}
            done({ok:true, stage:'authorize_url', url});
          } catch (e) {
            done({ok:false, stage:'exception', error:String(e && (e.stack || e.message) || e).slice(0, 700)});
          }
        })();
        """, email, did, auth_log_id) or {}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "invalid_result", "result": str(result)[:300]}
        if not result.get("ok"):
            return result
        authorize_url = str(result.get("url") or "").strip()
        parsed = urlparse(authorize_url)
        if parsed.scheme != "https" or parsed.hostname not in {"auth.openai.com", "chatgpt.com"}:
            return {"ok": False, "stage": "authorize_url", "reason": "unexpected_authorize_url"}
        _safe_get(
            driver,
            authorize_url,
            timeout=45,
            attempts=2,
            accept_hosts=("auth.openai.com", "chatgpt.com"),
            script_timeout=35,
        )
        return {"ok": True, "stage": "redirect", "navigated": True}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            driver.set_script_timeout(old_script_timeout)
        except Exception:
            pass


def _email_input_value_state(driver) -> dict:
    """读取当前可见邮箱框状态，用于提交后确认是否真的进入下一步。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .filter(visible)
          .map(el => ({type: el.getAttribute('type') || '', name: el.name || '', id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''}));
        return {url: location.href, inputs};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_login_page_still_present(driver) -> bool:
    state = _email_input_value_state(driver)
    return bool(state.get("inputs"))


def _terminal_registration_state(driver) -> str | None:
    """当前页面是否已处于注册/登录的终态。命中返回状态名，否则返回 None。

    供"邮箱提交后等待超时"与"重试前复检"使用：提交后撞 CF 时状态检测会一直挂起，
    CF 刚过、页面已推进到 OTP/密码/已登录时，复检能直接给出真实状态，
    避免掉到 email-verification 页后还去重填邮箱而报错。
    """
    try:
        if _has_access_token(driver):
            return "logged_in"
        if _is_login_password_page(driver):
            return "login_password"
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
    except Exception:
        pass
    return None


def _check_terminal_before_refill(driver) -> str | None:
    """重填邮箱前检查页面是否已推进到终态。

    登录密码页说明该邮箱已是已注册/不可用账号 → 直接抛错（与 _wait_email_submit_next_state
    返回 login_password 时的行为一致，错误文案被停用逻辑匹配）；OTP/密码/已登录 → 返回
    状态名，调用方直接走成功路径，绝不重复填邮箱。
    """
    state_name = _terminal_registration_state(driver)
    if state_name == "login_password":
        raise RuntimeError(
            f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}"
        )
    return state_name


def _wait_until_email_refill_safe(driver, timeout: int = _CF_TRANSITION_TIMEOUT) -> str:
    """Poll a challenge/navigation transition before allowing an email refill.

    Terminal registration states may return early.  An email page is only
    considered safe after the absolute transition deadline has elapsed.
    """
    timeout = max(_CF_TRANSITION_TIMEOUT, int(timeout or 0))
    end = time.time() + timeout
    cf_done = False
    last = None

    while time.time() < end:
        try:
            state_name = _check_terminal_before_refill(driver)
        except Exception as exc:
            if not _is_transient_navigation_error(exc, driver):
                raise
            state_name = None
        if state_name:
            return state_name

        active, cf_done, end = _cf_watch_tick(
            driver,
            end=end,
            label="邮箱重试前等待转场",
            done=cf_done,
        )
        if active:
            _fsleep(0.8)
            continue

        last = _email_input_value_state(driver)
        # A probe error or a page without the email form can both be a
        # navigation midpoint.  Neither authorizes refilling the form.
        _fsleep(0.8)

    try:
        final = _check_terminal_before_refill(driver)
    except Exception as exc:
        if not _is_transient_navigation_error(exc, driver):
            raise
        final = None
    if final:
        return final

    last = _email_input_value_state(driver)
    if not _probe_state_unsettled(last) and (last.get("inputs") or []):
        return "email_page"
    return "unknown"


def _wait_email_submit_next_state(driver, email: str, timeout: int = _CF_TRANSITION_TIMEOUT) -> str:
    """邮箱提交后等待进入 password / otp / logged_in；仍停留邮箱页则返回 email_page。

    Cloak/Playwright 路径里，点击 submit 后页面经常先发生一次 SPA 导航：
    `chatgpt.com/auth/login?email=...`，同时 React 会短暂把 email input 清空。
    旧逻辑一看到空 input 就立刻返回 `email_cleared`，导致在真正跳到
    `auth.openai.com/...` 前过早重填，形成“提交 -> 清空 -> 重填”的循环。
    这里对 email_cleared 做去抖：只记录并继续观察到绝对截止时间；若期间进入
    password/otp/login_password/logged_in 则按真实状态返回，截止后才让上层重试。
    """
    timeout = max(_CF_TRANSITION_TIMEOUT, int(timeout or 0))
    end = time.time() + timeout
    last = None
    stuck_seen_at: float | None = None
    stuck_last_log_at = 0.0
    native_recover_done = False
    nextauth_recover_done = False
    expected_email = str(email or "").strip().lower()
    cf_done = False
    while time.time() < end:
        # Cloudflare/Turnstile 挑战优先：挑战页上状态检测会误判，先自动解决。
        active, cf_done, end = _cf_watch_tick(driver, end=end, label="邮箱提交后等待跳转", done=cf_done)
        if active:
            _fsleep(0.8)
            continue
        if _has_access_token(driver):
            return "logged_in"
        if _is_login_password_page(driver):
            return "login_password"
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        state = _email_input_value_state(driver)
        last = state
        inputs = state.get("inputs") or []
        values = [str(i.get("value") or "") for i in inputs]
        url = str(state.get("url") or "")
        lower_url = url.lower()
        stuck_login_query = "/auth/login" in lower_url and "email=" in lower_url
        if stuck_login_query:
            has_blank = any(v == "" for v in values)
            has_expected = any(v.strip().lower() == expected_email for v in values)
            now = time.time()
            if stuck_seen_at is None:
                stuck_seen_at = now
            elapsed = now - stuck_seen_at
            if now - stuck_last_log_at > 2.0:
                logger.info(
                    "%s 邮箱提交后仍停留在 login?email，继续恢复转场：elapsed=%.1fs inputs=%s blank=%s expected=%s",
                    _log_prefix(driver), elapsed, len(inputs), has_blank, has_expected,
                )
                stuck_last_log_at = now
            if has_blank and not has_expected and not native_recover_done and elapsed >= 2.0:
                recover = _recover_email_submit_if_stuck(driver, email)
                native_recover_done = True
                logger.info("%s login?email 输入框已清空，原生补交一次表单：%s", _log_prefix(driver), recover)
            if not nextauth_recover_done and elapsed >= 5.0:
                fallback = _submit_email_via_browser_nextauth(driver, email)
                nextauth_recover_done = True
                safe_fallback = {
                    key: fallback.get(key)
                    for key in ("ok", "stage", "status", "reason", "navigated")
                    if key in fallback
                }
                logger.info("%s login?email 仍未推进，执行一次浏览器内登录跳转兜底：%s", _log_prefix(driver), safe_fallback)
                if fallback.get("ok"):
                    end = max(end, time.time() + 15.0)
        else:
            stuck_seen_at = None
        # 仍是当前邮箱页或导航中间态，继续短等。
        _fsleep(0.8)
    logger.info("%s 邮箱提交后等待下一步超时，最后邮箱页状态=%s", _log_prefix(driver), last)
    # 超时前做一次终态复检：CF 可能刚自动通过、页面已在 OTP/密码页，只是检测循环恰好结束。
    final = _terminal_registration_state(driver)
    if final:
        logger.info("%s 超时复检发现已进入终态：%s（不再视为邮箱页）", _log_prefix(driver), final)
        return final
    return "email_page" if _is_email_login_page_still_present(driver) else "unknown"


def _submit_email_and_wait_next(driver, email: str, attempts: int = 3) -> str:
    """填写并提交邮箱，必须确认进入 password/otp/logged_in 才返回。"""
    last_state = None
    transition_pending = False
    for attempt in range(1, attempts + 1):
        # 上一轮等待可能因 CF 超时而退出、但 CF 刚自动通过、页面已推进：
        # 先复检是否已进入终态，绝不在 email-verification 等非邮箱页上重填。
        try:
            state_name = _check_terminal_before_refill(driver)
        except Exception as exc:
            if not _is_transient_navigation_error(exc, driver):
                raise
            state_name = None
            transition_pending = True
        if state_name:
            logger.info("%s 重试前检测到已进入终态：%s（跳过重填，attempt=%s/%s）", _log_prefix(driver), state_name, attempt, attempts)
            return state_name
        cf_detected = _cf_active(driver)
        if transition_pending or cf_detected:
            logger.info("%s 重试前页面仍在 CF/导航转场，等待状态稳定", _log_prefix(driver))
            state_name = _wait_until_email_refill_safe(driver, timeout=_CF_TRANSITION_TIMEOUT)
            if state_name in ("password", "otp", "logged_in"):
                logger.info("%s 转场后已进入终态：%s（跳过重填，attempt=%s/%s）", _log_prefix(driver), state_name, attempt, attempts)
                return state_name
            if state_name != "email_page":
                transition_pending = True
                last_state = {"transition": state_name}
                logger.warning("%s 转场截止后仍未确认稳定邮箱页，不执行重填：%s", _log_prefix(driver), state_name)
                continue
            transition_pending = False
        try:
            _type_email_address(driver, email, timeout=20)
        except Exception as exc:
            if not _is_transient_navigation_error(exc, driver):
                raise
            transition_pending = True
            last_state = {"error": f"{type(exc).__name__}: {exc}"}
            logger.info("%s 填写邮箱时页面发生导航，返回转场轮询", _log_prefix(driver))
            continue
        state = _email_input_value_state(driver)
        last_state = state
        values = [str(i.get("value") or "") for i in (state.get("inputs") or [])]
        if not any(v.strip().lower() == email.strip().lower() for v in values):
            transition_pending = _probe_state_unsettled(state) or not bool(state.get("inputs"))
            logger.warning("%s 邮箱写入校验失败，准备重试：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, state)
            _fsleep(0.8)
            continue
        logger.info("%s 已填写邮箱并校验通过：%s", _log_prefix(driver), email)
        human_delay("form")
        # 交互式 Turnstile 需要先点 checkbox 生成 token，提交才会放行；隐形模式这里几乎不耗时。
        _solve_cf(driver, timeout=_CF_TRANSITION_TIMEOUT, label="邮箱提交前")
        _submit_email_step(driver, email)
        logger.info("%s 已提交邮箱，等待进入密码页或验证码页（%s/%s）", _log_prefix(driver), attempt, attempts)
        state_name = _wait_email_submit_next_state(driver, email, timeout=_CF_TRANSITION_TIMEOUT)
        if state_name == "login_password":
            raise RuntimeError(f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}")
        if state_name in ("password", "otp", "logged_in"):
            logger.info("%s 邮箱提交后已进入下一步：%s", _log_prefix(driver), state_name)
            return state_name
        transition_pending = state_name == "unknown"
        last_state = {"transition": state_name, "page": _email_input_value_state(driver)}
        logger.warning("%s 邮箱提交后仍未进入下一步：%s，等待下轮复检 state=%s", _log_prefix(driver), state_name, last_state["page"])
        _fsleep(1.0)
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={last_state}")


def _type_otp(driver, code: str) -> None:
    from selenium.webdriver.common.by import By

    # 密码页没有 OTP 控件。先做状态闸门，避免页面转场慢时把真实的密码页问题
    # 误报成“找不到 OTP 输入框”，也避免继续轮询邮箱验证码。
    try:
        if _is_login_password_page(driver) or _is_signup_password_page(driver):
            state = _password_page_state(driver)
            raise RuntimeError(f"密码页尚未完成，未进入 OTP 输入阶段: state={state}")
    except RuntimeError:
        raise
    except Exception as exc:
        logger.debug("%s[OTP] 密码页状态闸门探测暂不可用，继续由 OTP 控件探测：%s", _log_prefix(driver), str(exc)[:160])

    # 单输入框
    for selector in [
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[type='tel']",
    ]:
        els = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(e)]
        if len(els) == 1:
            _human_type_text(driver, els[0], code, clear=True)
            return

    # 6 个分格输入框
    boxes = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if _visible(e)]
    numeric_boxes = []
    for e in boxes:
        attrs = " ".join(str(e.get_attribute(k) or "") for k in ("inputmode", "autocomplete", "aria-label", "name", "id", "type"))
        if any(x in attrs.lower() for x in ("numeric", "one-time", "code", "otp", "tel")):
            numeric_boxes.append(e)
    if len(numeric_boxes) >= len(code):
        for e, ch in zip(numeric_boxes, code):
            if _browser_actions_enabled():
                _human_scroll_to(driver, e)
                _fsleep(random.uniform(0.04, 0.18))
            e.send_keys(ch)
            if _browser_actions_enabled():
                human_delay("keystroke")
        return

    raise RuntimeError("找不到 OTP 输入框")


def _email_otp_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', inputmode: el.getAttribute('inputmode') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const buttons = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', value: el.getAttribute('value') || '',
          action: el.getAttribute('data-dd-action-name') || '', aria: el.getAttribute('aria-label') || '',
          disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
        }));
        const errors = [...document.querySelectorAll('.react-aria-FieldError,[slot="errorMessage"],[id$="-error"],[aria-invalid="true"] + *,[class*="error"]')]
          .filter(visible).map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        return {url: location.href, title: document.title, inputs, buttons, errors, text: (document.body?.innerText || '').slice(0, 1200)};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, 'current_url', ''), "error": f"{type(exc).__name__}: {exc}"}


def _email_otp_snapshot_is_verification(state: dict | None) -> bool:
    state = state or {}
    url = str(state.get("url") or "").lower()
    if "/log-in/password" in url:
        return False
    if "email-verification" in url:
        return True
    attrs = " ".join(
        " ".join(str(i.get(k) or "") for k in ("type", "name", "id", "autocomplete", "inputmode"))
        for i in (state.get("inputs") or [])
    ).lower()
    return "one-time-code" in attrs or "otp" in attrs or "code" in attrs


def _is_email_verification_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return False
    if 'email-verification' in url:
        return True
    state = _email_otp_page_state(driver)
    return _email_otp_snapshot_is_verification(state)


def _clear_otp_inputs(driver) -> None:
    try:
        driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).filter(el => {
          const attrs = [el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label')].join(' ').toLowerCase();
          return /one-time|otp|code|numeric|tel/.test(attrs);
        });
        for (const el of inputs) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, ''); else el.value = '';
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """)
    except Exception:
        pass


def _click_resend_email_otp(driver, timeout: int = 20) -> dict:
    """点击重新发送邮箱验证码。优先按 DOM 属性识别，文本仅兜底。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const candidates = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=button],input[type=submit]')].filter(visible);
            const attrHit = candidates.find(el => {
              if (!enabled(el)) return false;
              const attrs = [el.id, el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
                .join(' ').toLowerCase();
              const name = String(el.getAttribute('name') || '').toLowerCase();
              const value = String(el.getAttribute('value') || '').toLowerCase();
              if (name === 'intent' && value === 'resend') return true;
              return /resend|send.*new|new.*code|again/.test(attrs);
            });
            if (attrHit) return attrHit;
            // 兜底：多语言文本，避免因页面没有稳定属性时卡死。
            return candidates.find(el => enabled(el) && /resend|send\s+(?:a\s+)?new\s+code|send\s+again|重新发送|重新发送电子邮件|重发|再次发送|再送信|新しい|届かない/.test((el.innerText || el.textContent || '').toLowerCase())) || null;
            """)
            if btn:
                text = str(btn.text or btn.get_attribute('value') or btn.get_attribute('data-dd-action-name') or '').strip()
                _human_click(driver, btn, label="resend_otp")
                logger.info("%s[OTP] 已点击重新发送验证码按钮：%s", _log_prefix(driver), text or '-')
                _fsleep(random.uniform(1.1, 2.4) if _browser_actions_enabled() else 1.5)
                return {"ok": True, "text": text}
        except Exception as exc:
            last = exc
        _fsleep(0.5)
    raise RuntimeError(f"找不到可点击的重新发送验证码按钮: last={last}, state={_email_otp_page_state(driver)}")


def _wait_after_email_otp_submit(driver, timeout: int = _CF_TRANSITION_TIMEOUT) -> str:
    """提交 OTP 后等待页面离开验证码页；仍在验证码页且有错误/输入框则认为验证码无效。"""
    timeout = max(_CF_TRANSITION_TIMEOUT, int(timeout or 0))
    end = time.time() + timeout
    last = {}
    cf_done = False
    accepted_polls = 0
    while time.time() < end:
        _fsleep(0.5)
        active, cf_done, end = _cf_watch_tick(driver, end=end, label="OTP 提交后", done=cf_done)
        if active:
            accepted_polls = 0
            continue
        last = _email_otp_page_state(driver)
        if _probe_state_unsettled(last):
            accepted_polls = 0
            continue
        if not _email_otp_snapshot_is_verification(last):
            accepted_polls += 1
            if accepted_polls >= 2:
                return 'accepted'
            continue
        accepted_polls = 0
        invalid = any(str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or []))
        if invalid or (last.get('errors') or []):
            return 'invalid'
    last = _email_otp_page_state(driver)
    if _probe_state_unsettled(last) or _email_otp_snapshot_is_verification(last):
        logger.warning("%s[OTP] 提交后转场等待到期，按验证码无效/过期处理 snapshot=%s", _log_prefix(driver), last)
        return 'invalid'
    return 'accepted'


def _click_continue(driver) -> None:
    _click_any(driver, [
        "button[type='submit']",
        "//button[contains(., 'Continue')]",
        "//button[contains(., '继续')]",
        "//button[contains(., 'Sign up')]",
        "//button[contains(., 'Create')]",
        "//button[contains(., 'Next')]",
    ], timeout=20)


def _maybe_accept(driver) -> None:
    # 页面刚加载可能先落在 Cloudflare 整页挑战或交互式 Turnstile 上；
    # 先自动解决，再处理 cookie/consent 弹层。无挑战时几乎不耗时。
    _solve_cf(driver, timeout=30, label="页面加载后的 Cloudflare 验证")
    # 只处理明确的 cookie/consent 弹层按钮；不要用 “Continue” 兜底，
    # 非日本出口时 “Continue with Google” 也会命中，导致误点 Google 登录。
    for selectors in ([
        "button#onetrust-accept-btn-handler",
        "button[data-testid='cookie-accept']",
        "button[data-testid='accept-cookies']",
        "//button[contains(., 'Accept')]",
        "//button[contains(., '同意')]",
        "//button[contains(., 'Agree')]",
    ],):
        try:
            _click_any(driver, selectors, timeout=3)
            _fsleep(0.5)
        except Exception:
            pass


def _page_snapshot(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const inputs = [...document.querySelectorAll('input,select,textarea')].map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
          autocomplete: el.getAttribute('autocomplete') || '', aria: el.getAttribute('aria-label') || '',
          value: el.value || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).filter(x => x.visible).slice(0, 30);
        const buttons = [...document.querySelectorAll('button,a[role=button],input[type=submit]')].map(el => ({
          text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
          type: el.getAttribute('type') || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
          disabled: !!el.disabled
        })).filter(x => x.visible).slice(0, 30);
        const widgets = [...document.querySelectorAll('[role=spinbutton], .react-aria-Select, [data-testid="hidden-select-container"] select, [data-type="year"], [data-type="month"], [data-type="day"]')].map(el => ({
          tag: el.tagName, role: el.getAttribute('role') || '', dataType: el.getAttribute('data-type') || '',
          aria: el.getAttribute('aria-label') || '', text: (el.innerText || el.textContent || '').trim().slice(0, 80),
          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 2000), inputs, buttons, widgets};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _has_access_token(driver) -> bool:
    try:
        result = driver.execute_async_script(r"""
        const done = arguments[0];
        fetch('https://chatgpt.com/api/auth/session', {credentials:'include'})
          .then(r => r.json()).then(j => done(Boolean(j && j.accessToken)))
          .catch(() => done(false));
        """)
        return bool(result)
    except Exception:
        return False


def _is_profile_like(snapshot: dict) -> bool:
    """资料页识别：兼容 about-you/profile；年龄/生日控件可能不是 input，而是 React Aria widget。"""
    url = str(snapshot.get('url') or '').lower()
    inputs = snapshot.get('inputs') or []
    widgets = snapshot.get('widgets') or []
    attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('name', 'id', 'placeholder', 'autocomplete', 'aria', 'type')).lower()
        for i in inputs
    )
    widget_attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('role', 'dataType', 'aria', 'text', 'tag')).lower()
        for i in widgets
    )
    has_profile_url = any(x in url for x in ('about-you', 'profile', 'signup/profile', 'create-account/profile'))
    has_name_field = (
        'autocomplete name' in attrs
        or ' name ' in f' {attrs} '
        or 'fullname' in attrs
        or 'full_name' in attrs
        or 'firstname' in attrs
        or 'lastname' in attrs
    )
    has_age_or_birth_field = any(x in f' {attrs} {widget_attrs} ' for x in (
        ' age', '-age', '_age', 'birth', 'birthday', 'birthdate',
        ' month', '-month', '_month', 'data-type month',
        ' day', '-day', '_day', 'data-type day',
        ' year', '-year', '_year', 'data-type year',
        'spinbutton', 'react-aria-select', 'type number',
    ))
    # about-you/profile URL 本身已经足够强；部分新版页面会用无 name 的 React Aria 控件。
    return has_profile_url and (has_name_field or has_age_or_birth_field or bool(inputs) or bool(widgets))


def _set_element_value(driver, el, value: str) -> None:
    """兼容 React 受控输入框：用原生 setter 设置值并派发 input/change。"""
    driver.execute_script(r"""
    const el = arguments[0];
    const value = String(arguments[1]);
    const tag = (el.tagName || '').toLowerCase();
    el.scrollIntoView({block:'center'});
    el.focus();
    if (tag === 'select') {
      el.value = value;
    } else {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.blur();
    """, el, value)


def _select_or_type(driver, selectors: list[str], value: str, timeout: int = 3) -> bool:
    try:
        el = _find_any(driver, selectors, timeout=timeout)
    except Exception:
        return False
    try:
        tag = (el.tag_name or '').lower()
        if tag == 'select':
            if el.__class__.__name__ == 'CloakElement':
                driver.execute_script(r"""
                const el = arguments[0], value = String(arguments[1]);
                const n = parseInt(value, 10);
                const opts = [...el.options];
                const match = opts.find(o => o.value === value)
                  || opts.find(o => (o.textContent || '').trim() === value)
                  || opts[Math.max(0, n - 1)];
                if (match) el.value = match.value; else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """, el, str(value))
            else:
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_value(str(int(value)))
                except Exception:
                    try:
                        sel.select_by_visible_text(str(int(value)))
                    except Exception:
                        # 月份 select 可能是 0-based，也可能是 1-based；先 value/text，不行再 index。
                        sel.select_by_index(max(0, int(value)-1))
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", el)
        else:
            _human_type_text(driver, el, str(value), clear=True)
        return True
    except Exception as exc:
        logger.debug('%s 填写字段失败 selectors=%s value=%s err=%s', _log_prefix(driver), selectors, value, exc)
        return False


def _fill_birthday_or_age(driver, birthday: str, age: int) -> str | None:
    """填写 about-you 的年龄/生日控件。

    参考 FlowPilot：优先处理直接年龄 input；否则兼容 hidden birthday/date、原生年月日
    select/input、React Aria hidden native select、role=spinbutton[data-type=year/month/day]。
    返回 age / birthday / ymd / react_select / spinbutton / None。
    """
    y, m, d = birthday.split('-')
    result = driver.execute_script(r"""
    const birthday = String(arguments[0]);
    const year = String(arguments[1]);
    const month = String(Number(arguments[2]));
    const month2 = String(arguments[2]).padStart(2, '0');
    const day = String(Number(arguments[3]));
    const day2 = String(arguments[3]).padStart(2, '0');
    const age = String(arguments[4]);
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const setValue = (el, value) => {
      if (!el) return false;
      el.scrollIntoView?.({block:'center'});
      el.focus?.();
      const tag = (el.tagName || '').toLowerCase();
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
        : tag === 'select' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, String(value)); else el.value = String(value);
      if (tag === 'select') {
        [...el.options].forEach(opt => { opt.selected = String(opt.value) === String(value); });
      }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.blur?.();
      return true;
    };
    const isDateish = el => {
      const h = [el.getAttribute('data-testid') || '', el.getAttribute('data-type') || '',
                 el.getAttribute('aria-label') || '', el.id || '', el.name || ''].join(' ').toLowerCase();
      return /(year|month|day|birth|age|年|月|日|年龄|年齢)/.test(h);
    };
    const ageInput = [...document.querySelectorAll('input[name="age"], input#age, input[id$="-age"]')].find(visible)
      || [...document.querySelectorAll('input[type="number"]')].find(el => visible(el) && !isDateish(el));
    if (ageInput && setValue(ageInput, age)) return {ok:true, mode:'age'};

    const dateInput = [...document.querySelectorAll('input[name="birthdate"], input[type="date"], input[name="birthday"]')]
      .find(el => visible(el) || String(el.getAttribute('type') || '').toLowerCase() === 'date');
    if (dateInput && setValue(dateInput, birthday)) return {ok:true, mode:'birthday'};

    const setFirst = (selectors, values) => {
      for (const sel of selectors) {
        for (const el of [...document.querySelectorAll(sel)]) {
          if (!visible(el)) continue;
          for (const val of values) {
            if (el.tagName === 'SELECT') {
              const has = [...el.options].some(o => String(o.value) === String(val) || String(o.textContent || '').trim() === String(val));
              if (!has) continue;
            }
            if (setValue(el, val)) return true;
          }
        }
      }
      return false;
    };
    const yOk = setFirst(['select[name="year"]','input[name="year"]','select[id*="year"]','input[id*="year"]'], [year]);
    const mOk = setFirst(['select[name="month"]','input[name="month"]','select[id*="month"]','input[id*="month"]'], [month, month2]);
    const dOk = setFirst(['select[name="day"]','input[name="day"]','select[id*="day"]','input[id*="day"]'], [day, day2]);
    if (yOk && mOk && dOk) {
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'ymd'};
    }

    // React Aria Select 通常有 hidden native select；不依赖标签文字，按 option 数值范围和 DOM 顺序推断年/月/日。
    const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select, .react-aria-Select select, select')]
      .filter(el => !el.disabled);
    const nums = sel => [...sel.options].map(o => Number(o.value)).filter(Number.isFinite);
    const maxNum = sel => Math.max(...nums(sel), -Infinity);
    const minNum = sel => Math.min(...nums(sel), Infinity);
    const hasOption = (sel, val) => [...sel.options].some(o => String(o.value) === String(val));
    const yearSelects = selects.filter(sel => hasOption(sel, year) && maxNum(sel) > 1900);
    const smallSelects = selects.filter(sel => !yearSelects.includes(sel));
    const monthSelects = smallSelects.filter(sel => (hasOption(sel, month) || hasOption(sel, month2)) && minNum(sel) <= 1 && maxNum(sel) <= 12);
    const daySelects = smallSelects.filter(sel => (hasOption(sel, day) || hasOption(sel, day2)) && maxNum(sel) >= 28);
    if (yearSelects.length && monthSelects.length && daySelects.length) {
      const ys = yearSelects[0];
      let ms = monthSelects[0];
      let ds = daySelects.find(x => x !== ms) || daySelects[0];
      setValue(ys, year);
      setValue(ms, hasOption(ms, month) ? month : month2);
      setValue(ds, hasOption(ds, day) ? day : day2);
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'react_select'};
    }

    const spinYear = document.querySelector('[role="spinbutton"][data-type="year"]');
    const spinMonth = document.querySelector('[role="spinbutton"][data-type="month"]');
    const spinDay = document.querySelector('[role="spinbutton"][data-type="day"]');
    if (spinYear && spinMonth && spinDay) return {ok:false, mode:'spinbutton_needed'};
    // React Aria DateField：segment 可能是 data-testid/data-type/aria-label 的
    // input/contenteditable/div（新版 OpenAI 生日控件），JS 直接 setValue 不可靠，
    // 只要页面有日期 segment 就标记让 Python 用 Selenium send_keys（真实键盘输入）填写。
    const hasDateSeg = [...document.querySelectorAll(
      '[data-testid*="year"],[data-testid*="month"],[data-testid*="day"],'
      + '[data-type*="year"],[data-type*="month"],[data-type*="day"],'
      + '[aria-label*="Year" i],[aria-label*="Month" i],[aria-label*="Day" i],'
      + '[aria-label*="年"],[aria-label*="月"],[aria-label*="日"],'
      + '[role="spinbutton"]'
    )].some(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    if (hasDateSeg) return {ok:false, mode:'datefield_needed'};
    return {ok:false, mode:'missing'};
    """, birthday, y, m, d, str(age)) or {}
    if result.get('ok'):
        return str(result.get('mode') or 'birthday')
    if result.get('mode') not in ('spinbutton_needed', 'datefield_needed'):
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        mod = Keys.COMMAND
        try:
            import platform
            if platform.system().lower() != 'darwin':
                mod = Keys.CONTROL
        except Exception:
            pass
        # React Aria DateField / spinbutton：data-testid / data-type / aria-label 多种结构，
        # segment 可能是 input / contenteditable / 容器（内部再包 input）。
        date_parts = [
            ('year', y),
            ('month', str(m).zfill(2)),
            ('day', str(d).zfill(2)),
        ]
        segs = []
        for label, value in date_parts:
            el = _find_date_segment(driver, label)
            if el is None:
                logger.warning('%s 生日控件缺少 %s segment，跳过', _log_prefix(driver), label)
                return None
            segs.append((el, value))
        for el, value in segs:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", el)
            except Exception:
                pass
            _fsleep(0.1)
            try:
                el.click()
            except Exception:
                pass
            _fsleep(0.05)
            el.send_keys(mod, 'a')
            _fsleep(0.05)
            el.send_keys(str(value))
            _fsleep(0.1)
            try:
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true})); arguments[0].blur();", el)
            except Exception:
                pass
        driver.execute_script(r"""
        const hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
          const value = arguments[0];
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(hidden, value); else hidden.value = value;
          hidden.dispatchEvent(new Event('input', {bubbles:true}));
          hidden.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """, birthday)
        return 'datefield'
    except Exception as exc:
        logger.debug('%s 生日控件填写失败：%s', _log_prefix(driver), exc)
        return None


def _find_date_segment(driver, label: str):
    """按 label(year/month/day) 定位 React Aria 生日 segment，返回可输入元素。

    覆盖多种结构：
      [data-testid="year"] / [data-testid="Year"] / [data-type="year"] /
      [aria-label*="Year" i] / [role="spinbutton"][data-type="year"]
    segment 可能是容器（内部再包 input），也返回内部 input。
    """
    from selenium.webdriver.common.by import By

    cap = label.capitalize()  # Year / Month / Day
    candidates = [
        f'[data-testid="{label}"]',
        f'[data-testid="{cap}"]',
        f'[data-type="{label}"]',
        f'[aria-label*="{cap}" i]',
        f'[aria-label*="{label}" i]',
        f'[role="spinbutton"][data-type="{label}"]',
    ]
    for sel in candidates:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_displayed():
                    continue
                return _segment_input(driver, el)
            except Exception:
                continue
    return None


def _segment_input(driver, el):
    """segment 容器 → 返回内部可输入元素（input），否则返回元素自身。"""
    from selenium.webdriver.common.by import By

    try:
        inner = el.find_element(By.TAG_NAME, "input")
        try:
            if inner.is_displayed() and not inner.get_attribute("readonly"):
                return inner
        except Exception:
            pass
    except Exception:
        pass
    return el


def _generate_roxy_password() -> str:
    """注册密码：大小写字母 + 数字 + 符号，14 位，各字符类至少 1 个。

    create-account/password 页校验要求"至少 12 位、含字母、符号、数字"，因此必须含符号。
    符号集排除 -（发货格式 ---- 分隔符冲突）、引号、斜杠等易混淆/破坏格式的字符。
    """
    upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    lower = 'abcdefghjkmnpqrstuvwxyz'
    digits = '23456789'
    symbols = '!@#$%^&*'
    groups = [upper, lower, digits, symbols]
    all_chars = ''.join(groups)
    chars = [random.choice(g) for g in groups]
    while len(chars) < 14:
        chars.append(random.choice(all_chars))
    random.shuffle(chars)
    return ''.join(chars)


def _registration_password() -> str:
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, 'REGISTER_PASSWORD', '') or '').strip()
        if configured:
            return configured
    except Exception:
        pass
    return _generate_roxy_password()


def _register_set_password_enabled() -> bool:
    """注册时是否设置 ChatGPT 登录密码（不走 passwordless 旁路）。默认 True。"""
    try:
        from config import register as _register_cfg
        return bool(getattr(_register_cfg, 'REGISTER_SET_PASSWORD', True))
    except Exception:
        return True


def _password_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', visible: visible(el), value: el.type === 'password' ? '<password>' : (el.value || '')
        })).slice(0, 30);
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const buttons = [...document.querySelectorAll('button,input[type="submit"]')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          value: el.getAttribute('value') || '', aria: el.getAttribute('aria-label') || '',
          title: el.getAttribute('title') || '', href: el.getAttribute('href') || '',
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120),
          disabled: !!el.disabled, visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, inputs, forms, buttons};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_signup_password_page(driver) -> bool:
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    if any(x in url for x in ('/create-account/password', '/u/signup/password', '/signup/password')):
        return True
    if '/log-in/password' in url:
        return False
    inputs = state.get('inputs') or []
    return any(
        i.get('visible') and (
            str(i.get('type') or '').lower() == 'password'
            or 'password' in str(i.get('name') or '').lower()
            or str(i.get('autocomplete') or '').lower() == 'new-password'
        )
        for i in inputs
    )


def _is_login_password_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return True
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    if '/log-in/password' in url:
        return True
    if any(x in url for x in ('/create-account/password', '/u/signup/password', '/signup/password')):
        return False
    inputs = state.get('inputs') or []
    visible_passwords = [
        item for item in inputs
        if item.get('visible') and (
            str(item.get('type') or '').lower() == 'password'
            or 'password' in str(item.get('name') or '').lower()
            or 'password' in str(item.get('autocomplete') or '').lower()
        )
    ]
    if not visible_passwords:
        return False
    if any(str(item.get('autocomplete') or '').lower() == 'new-password' for item in visible_passwords):
        return False
    return any(
        str(item.get('autocomplete') or '').lower() == 'current-password'
        for item in visible_passwords
    ) or '/auth/login' in url or '/log-in' in url


def _click_passwordless_signup_if_present(driver) -> dict:
    """
    新版注册/登录流在 password 页可能默认要求密码。
    如果页面提供“使用一次性验证码”按钮，优先点击进入邮箱 OTP 页面。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')].filter(el => visible(el) && enabled(el));
        const isPasswordlessOtp = el => {
          const name = String(el.getAttribute('name') || '').toLowerCase();
          const value = String(el.getAttribute('value') || '').toLowerCase();
          const attrs = [
            el.id, name, value, el.getAttribute('aria-label'), el.getAttribute('title'),
            el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.className, el.textContent
          ].join(' ').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('value') || '');
          return (
            (name === 'intent' && value.includes('passwordless') && value.includes('send_otp')) ||
            (name === 'intent' && value.includes('passwordless') && value.includes('otp')) ||
            (name === 'intent' && value === 'passwordless_signup_send_otp') ||
            (name === 'intent' && value === 'passwordless_login_send_otp') ||
            attrs.includes('passwordless_signup_send_otp') ||
            attrs.includes('passwordless_login_send_otp') ||
            /passwordless.*otp|otp.*passwordless|one[-_\s]?time.*code|code.*one[-_\s]?time|use\s+(?:another|a\s+different)\s+(?:method|way)|try\s+another\s+way|use\s+code\s+instead|(?:email|e-mail)\s+(?:verification\s+)?code|sign\s+in\s+with\s+(?:a\s+)?code|continue\s+with\s+(?:email|code)/.test(attrs) ||
            text.includes('使用一次性验证码注册') ||
            text.includes('使用一次性验证码登录') ||
            text.includes('使用一次性验证码') ||
            text.includes('使用一次性驗證碼註冊') ||
            text.includes('使用一次性驗證碼登入') ||
            text.includes('一次性验证码') ||
            text.includes('一次性驗證碼') ||
            text.includes('メールでコード') ||
            text.includes('ワンタイムコード') ||
            text.includes('認証コード') ||
            text.includes('useonetimeregistrationcode') ||
            text.includes('useaone-timecodetosignup') ||
            text.includes('useaone-timecodetoregister') ||
            text.includes('useaone-timecodetologin') ||
            text.includes('continuewithaone-timecode') ||
            text.includes('loginwithaone-timecode') ||
            text.includes('signupwithaone-timecode') ||
            text.includes('one-timecode')
          );
        };
        const btn = candidates.find(isPasswordlessOtp);
        if (!btn) return {ok:false, reason:'missing_passwordless_button'};
        btn.scrollIntoView({block:'center'});
        return {
          ok:true,
          reason:'passwordless_send_otp_target',
          button: btn,
          name: btn.getAttribute('name') || '',
          value: btn.getAttribute('value') || '',
          text: (btn.textContent || '').trim().slice(0, 80)
        };
        """) or {"ok": False, "reason": "empty_result"}
        if result.get("ok") and result.get("button"):
            _human_click(driver, result.get("button"), label="passwordless_otp")
            result["reason"] = "clicked_passwordless_send_otp"
            result.pop("button", None)
        return result
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _wait_for_otp_or_session_after_password_action(driver, timeout: int = _PASSWORD_TRANSITION_TIMEOUT, label: str = "密码页") -> str:
    """等待密码页动作的正向结果。

    只有明确检测到邮箱验证码页或登录态才返回；页面仍在密码页、导航尚未稳定、
    或探测失败时都继续等待，截止后抛出带状态的错误，禁止调用方盲目填写 OTP。
    """
    end = time.time() + max(_PASSWORD_TRANSITION_TIMEOUT, int(timeout or 0))
    cf_done = False
    last = {}
    while time.time() < end:
        active, cf_done, end = _cf_watch_tick(driver, end=end, label=label, done=cf_done)
        if active:
            _fsleep(1)
            continue
        if _is_email_verification_page(driver):
            return "otp"
        if _has_access_token(driver):
            return "logged_in"
        last = _email_otp_page_state(driver)
        if _probe_state_unsettled(last):
            _fsleep(0.5)
            continue
        if _email_otp_snapshot_is_verification(last):
            return "otp"
        _fsleep(0.5)
    raise RuntimeError(f"{label}动作后未进入 OTP/登录态，仍需等待页面转场: state={last}")


def _click_continue_with_password(driver, timeout: int = 20) -> bool:
    """OTP 页（email-verification）点击"使用密码继续"，切换到密码注册流程。

    OpenAI 新版注册流对新邮箱直达 OTP 页（passwordless_primary），但 OTP 页上有
    "使用密码继续"入口（contactVerification.continueWithPassword）。它是链接：
    signup 会话跳 /create-account/password，login 跳 /log-in/password。点击后等待
    进入密码页。返回是否成功切换到密码页。

    这是"注册时设置密码"的关键一步：先切到密码页填密码，随后才会收到邮箱验证码。
    """
    clicked = driver.execute_async_script(r"""
        const done = arguments[arguments.length - 1];
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const norm = s => (s || '').toLowerCase().replace(/\s+/g, '');
        const byHref = [...document.querySelectorAll('a[href*="password"],button[href*="password"]')]
            .find(el => visible(el) && /(create-account\/password|log-in\/password)/.test(el.getAttribute('href') || ''));
        if (byHref) { byHref.click(); return done({ok:true, via:'href'}); }
        const texts = /(continue with password|用密码继续|使用密码继续|パスワードで続行|密码继续)/i;
        const byText = [...document.querySelectorAll('a,button')]
            .find(el => visible(el) && texts.test(norm(el.textContent || '')));
        if (byText) { byText.click(); return done({ok:true, via:'text'}); }
        return done({ok:false, reason:'no_continue_with_password'});
    """)
    if not (clicked or {}).get("ok"):
        logger.info("%s[密码] OTP 页未找到'使用密码继续'入口：%s", _log_prefix(driver), clicked)
        return False
    logger.info("%s[密码] 已点击'使用密码继续'（%s），等待进入密码页", _log_prefix(driver), (clicked or {}).get("via"))
    end = time.time() + timeout
    cf_done = False
    while time.time() < end:
        active, cf_done, end = _cf_watch_tick(driver, end=end, label="切换到密码页", done=cf_done)
        if active:
            _fsleep(1)
            continue
        if _is_signup_password_page(driver) or _is_login_password_page(driver):
            logger.info("%s[密码] 已切换到密码页：url=%s", _log_prefix(driver), str(driver.current_url)[:90])
            return True
        _fsleep(0.5)
    logger.info("%s[密码] 点击'使用密码继续'后未检测到密码页，url=%s", _log_prefix(driver), str(driver.current_url)[:90])
    return False


def _fill_password_page_if_present(driver, email: str, timeout: int = 25) -> str | None:
    """邮箱提交后兼容 create-account/password。返回本次设置的 OpenAI 账号密码；未遇到密码页返回 None。"""
    end = time.time() + timeout
    cf_done = False
    last = {}
    while time.time() < end:
        # 邮箱提交后可能先落在 Cloudflare/Turnstile 挑战上，密码页检测会误判；
        # 先尝试自动解决（无挑战时几乎不耗时）。
        active, cf_done, end = _cf_watch_tick(driver, end=end, label="密码页", done=cf_done)
        if active:
            _fsleep(1)
            continue
        if _is_email_verification_page(driver):
            return None
        if _has_access_token(driver):
            return None
        last = _password_page_state(driver)
        is_signup_password = _is_signup_password_page(driver)
        is_login_password = _is_login_password_page(driver)
        if not (is_signup_password or is_login_password):
            _fsleep(0.5)
            continue
        passwordless = None
        if is_login_password or not _register_set_password_enabled():
            # 登录密码页没有可填写的注册密码；无论配置如何，都先尝试页面提供的
            # 一次性验证码入口。找不到入口时必须停止，不能把密码页交给 OTP 阶段。
            passwordless = _click_passwordless_signup_if_present(driver)
        if passwordless and passwordless.get('ok'):
            logger.info("%s 检测到 password 页，已点击一次性验证码入口：email=%s detail=%s", _log_prefix(driver), email, passwordless)
            _wait_for_otp_or_session_after_password_action(driver, label="一次性验证码入口")
            logger.info("%s 一次性验证码入口已确认进入 OTP/登录态", _log_prefix(driver))
            return None
        if is_login_password:
            raise RuntimeError(
                f"已进入登录密码页且未找到一次性验证码入口，按已注册/不可用邮箱处理并停用: state={last}"
            )
        password = _registration_password()
        logger.info("%s 检测到 create-account/password，准备设置密码（%s 位）：email=%s", _log_prefix(driver), len(password), email)
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
          .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_submit'};
        buttons[0].el.scrollIntoView({block:'center'});
        return {ok:true, reason:'password_targets', input, button: buttons[0].el};
        """) or {}
        if not result.get('ok'):
            raise RuntimeError(f"密码页处理失败：{result} state={last}")
        _human_type_text(driver, result.get("input"), password, clear=True)
        human_delay("form", minimum=0.4, maximum=1.4)
        _human_click(driver, result.get("button"), label="password_submit")
        logger.info("%s 已填写并提交密码页", _log_prefix(driver))
        # 提交密码后可能经历 CF/授权跳转；至少等待完整转场窗口，不能在仍为密码页时
        # 直接返回密码并让调用方盲目进入 OTP 阶段。
        wait_end = time.time() + _PASSWORD_TRANSITION_TIMEOUT
        cf_done_pw = False
        transition_state = {}
        while time.time() < wait_end:
            active, cf_done_pw, wait_end = _cf_watch_tick(driver, end=wait_end, label="密码提交后", done=cf_done_pw)
            if active:
                _fsleep(1)
                continue
            if _is_email_verification_page(driver):
                logger.info("%s 密码提交后已进入邮箱验证码页", _log_prefix(driver))
                return password
            if _has_access_token(driver):
                logger.info("%s 密码提交后已检测到登录态", _log_prefix(driver))
                return password
            transition_state = _password_page_state(driver)
            if _is_login_password_page(driver):
                raise RuntimeError(
                    f"密码提交后进入登录密码页，按已注册/不可用邮箱处理并停用: state={transition_state}"
                )
            # 只有明确看到 OTP 页或登录态才返回；其它页面状态继续等待，避免导航
            # 短暂落后时误判为成功。
            _fsleep(0.5)
        final_state = _password_page_state(driver)
        if _is_email_verification_page(driver) or _has_access_token(driver):
            return password
        raise RuntimeError(f"密码提交后仍停留在密码页，未进入 OTP 输入页: url={getattr(driver, 'current_url', '')} state={final_state or transition_state}")
    logger.info("%s 未检测到密码页，继续后续流程 last=%s", _log_prefix(driver), last)
    return None


def _accept_profile_consents(driver) -> int:
    """about-you/profile 下出现韩国/日本个人信息同意协议时，默认全部勾选。

    不依赖可见文字；优先处理 allCheckboxes，再处理所有必选 consent checkbox。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const isChecked = el => el.checked === true || String(el.getAttribute('aria-checked') || el.closest('[role="checkbox"]')?.getAttribute('aria-checked') || '').toLowerCase() === 'true';
        const mark = el => {
          if (!el || isChecked(el)) return false;
          const label = el.closest('label');
          try {
            (label && visible(label) ? label : el).scrollIntoView({block:'center'});
            (label && visible(label) ? label : el).click();
          } catch (_) {}
          if (!isChecked(el)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
            if (setter) setter.call(el, true); else el.checked = true;
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return isChecked(el);
        };
        const all = [...document.querySelectorAll('input[type="checkbox"]')]
          .filter(el => visible(el) || visible(el.closest('label')));
        if (!all.length) return {count:0, names:[]};
        const byName = name => all.find(el => String(el.name || '').toLowerCase() === name.toLowerCase());
        const ordered = [];
        const add = el => { if (el && !ordered.includes(el)) ordered.push(el); };
        add(byName('allCheckboxes'));
        for (const name of ['personalInfoConsent', 'thirdPartyConsent', 'overseasTransferConsent']) add(byName(name));
        for (const el of all) {
          const n = String(el.name || '').toLowerCase();
          const id = String(el.id || '').toLowerCase();
          if (/consent|checkbox|agree|required|personal|third|overseas/.test(`${n} ${id}`)) add(el);
        }
        // about-you/profile 页面里的 checkbox 基本都是必选 consent；剩余可见 checkbox 也全部勾选。
        for (const el of all) add(el);
        const clicked = [];
        for (const el of ordered) {
          if (mark(el)) clicked.push(el.name || el.id || 'checkbox');
        }
        return {count: clicked.length, names: clicked};
        """) or {}
        count = int(result.get('count') or 0)
        if count:
            logger.info("%s 已勾选 about-you/profile 同意协议复选框：%s", _log_prefix(driver), result.get('names'))
        return count
    except Exception as exc:
        logger.debug('%s 勾选 profile consent 失败：%s', _log_prefix(driver), exc)
        return 0


def _dump_profile_dom(driver) -> None:
    """诊断：出生年月日控件识别失败时，dump 相关元素 DOM 便于精准定位新版控件结构。"""
    try:
        result = driver.execute_script(r"""
        const labels = ['birth','birthday','birthdate','year','month','day','生年月日','年齢','年龄','DateField','DateInput'];
        const nodes = [...document.querySelectorAll('input,select,textarea,[role="spinbutton"],[data-testid],[data-type]')]
          .filter(el => {
            const h = [el.id, el.name || '', el.getAttribute('data-testid') || '', el.getAttribute('data-type') || '',
                       el.getAttribute('aria-label') || '', el.placeholder || '', String(el.className || '')].join(' ').toLowerCase();
            return labels.some(x => h.includes(x.toLowerCase()));
          }).slice(0, 10);
        return nodes.map(el => {
          const r = el.getBoundingClientRect();
          return {tag: el.tagName, id: el.id, name: el.name || '', cls: String(el.className || '').slice(0,80),
                  testid: el.getAttribute('data-testid') || '', dtype: el.getAttribute('data-type') || '',
                  aria: el.getAttribute('aria-label') || '', role: el.getAttribute('role') || '',
                  placeholder: el.getAttribute('placeholder') || '',
                  contenteditable: el.isContentEditable, visible: r.width>0 && r.height>0,
                  value: String(el.value || '').slice(0, 40),
                  outer: (el.outerHTML || '').slice(0, 320)};
        });
        """)
        logger.warning("[Roxy注册][诊断] 出生年月日控件 DOM：%s", json.dumps(result, ensure_ascii=False)[:1800])
    except Exception as exc:
        logger.debug("%s 出生年月日控件 DOM dump 失败：%s", _log_prefix(driver), exc)


def _complete_profile_page(driver, name: str, birthday: str, timeout: int = 45) -> bool:
    """等待并完成姓名/生日页；若已经登录成功则返回 False，不把它当失败。"""
    end = time.time() + timeout
    y, m, d = birthday.split('-')
    from datetime import date
    today = date.today()
    age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    last_snapshot = {}
    dom_dumped = False
    cf_done = False
    while time.time() < end:
        _fsleep(1)
        active, cf_done, end = _cf_watch_tick(driver, end=end, label="资料页", done=cf_done)
        if active:
            continue
        if _has_access_token(driver):
            logger.info('%s 已检测到登录态，资料页可能已跳过', _log_prefix(driver))
            return False
        snap = _page_snapshot(driver)
        last_snapshot = snap
        if not _is_profile_like(snap):
            logger.info('%s 等待资料页中：url=%s', _log_prefix(driver), snap.get('url'))
            continue

        logger.info('%s 检测到资料页，开始填写姓名生日：url=%s inputs=%s', _log_prefix(driver), snap.get('url'), snap.get('inputs'))
        name_ok = False
        # 常见单姓名字段
        for selectors in [
            ["input[name='name']", "input[name='fullName']", "input[name='full_name']", "input[autocomplete='name']"],
            ["input[placeholder*='Name']", "input[placeholder*='name']", "input[aria-label*='Name']", "input[aria-label*='name']"],
        ]:
            if _select_or_type(driver, selectors, name, timeout=3):
                logger.info("%s 已填写姓名字段：%s", _log_prefix(driver), name)
                name_ok = True
                break
        # 兼容 first/last 分开
        if not name_ok:
            parts = name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else 'User'
            first_ok = _select_or_type(driver, ["input[name='firstName']", "input[name='first_name']", "input[placeholder*='First']", "input[aria-label*='First']"], first, timeout=2)
            last_ok = _select_or_type(driver, ["input[name='lastName']", "input[name='last_name']", "input[placeholder*='Last']", "input[aria-label*='Last']"], last, timeout=2)
            name_ok = first_ok or last_ok

        birth_mode = _fill_birthday_or_age(driver, birthday, age)
        birth_ok = bool(birth_mode)
        if birth_ok:
            if birth_mode == 'age':
                logger.info("%s 已填写年龄字段：%s", _log_prefix(driver), age)
            else:
                logger.info("%s 已填写生日字段 mode=%s value=%s", _log_prefix(driver), birth_mode, birthday)

        if not name_ok or not birth_ok:
            logger.warning('%s 资料页字段未填完整 name_ok=%s birth_ok=%s snapshot=%s', _log_prefix(driver), name_ok, birth_ok, snap)
            if not dom_dumped:
                dom_dumped = True
                _dump_profile_dom(driver)
            continue

        _accept_profile_consents(driver)
        human_delay('form')
        for _ in range(3):
            if _click_if_enabled_submit(driver):
                logger.info('%s 已点击资料页提交按钮，等待 OAuth 跳转', _log_prefix(driver))
                return True
            _fsleep(1)
        logger.warning('%s 找不到可点击的资料页提交按钮 snapshot=%s', _log_prefix(driver), _page_snapshot(driver))
    raise RuntimeError(f'等待/填写资料页超时，最后页面：{last_snapshot}')


def _click_if_enabled_submit(driver) -> bool:
    """提交资料页：优先 form.requestSubmit/button[type=submit]，不依赖按钮文字。"""
    try:
        target = driver.execute_script(r"""
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const forms = [...document.querySelectorAll('form')].filter(visible);
        for (const form of forms) {
          const submit = form.querySelector('button[type="submit"], input[type="submit"]');
          if (submit && visible(submit) && !submit.disabled) {
            submit.scrollIntoView({block:'center'});
            return submit;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return 'submitted_by_requestSubmit';
          }
        }
        const submitters = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .filter(el => visible(el) && !el.disabled);
        if (submitters.length) {
          submitters[0].scrollIntoView({block:'center'});
          return submitters[0];
        }
        // 兜底：页面只有一个可点击 button 时点击它，但仍不读文字。
        const buttons = [...document.querySelectorAll('button:not([disabled])')].filter(visible);
        if (buttons.length === 1) {
          buttons[0].scrollIntoView({block:'center'});
          return buttons[0];
        }
        return null;
        """)
        if not target:
            return False
        if isinstance(target, str):
            return True
        _human_click(driver, target, label="profile_submit")
        return True
    except Exception:
        return False


def _read_chatgpt_session_once(driver) -> dict | None:
    """当前页面必须在 chatgpt.com；读取 /api/auth/session，拿不到 token 返回 None。"""
    script = r"""
    const done = arguments[0];
    fetch('/api/auth/session', {credentials: 'include'})
      .then(r => r.json())
      .then(j => done({ok: true, data: j}))
      .catch(e => done({ok: false, error: String(e)}));
    """
    result = driver.execute_async_script(script)
    if result and result.get("ok"):
        data = result.get("data") or {}
        if data.get("accessToken"):
            logger.info("%s /api/auth/session 已返回 accessToken", _log_prefix(driver))
            return data
        logger.info("%s 等待 ChatGPT session 写入 accessToken，当前响应 keys=%s", _log_prefix(driver), list(data.keys()))
    return None


def _switch_to_chatgpt_window_if_any(driver) -> bool:
    """有些浏览器/适配层会在新窗口完成 callback；尝试切到已有 chatgpt.com 句柄。"""
    try:
        handles = list(getattr(driver, "window_handles", []) or [])
        current_handle = None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:
            current_handle = None
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in str(getattr(driver, "current_url", "") or ""):
                    return True
            except Exception:
                continue
        if current_handle is not None:
            try:
                driver.switch_to.window(current_handle)
            except Exception:
                pass
    except Exception:
        pass
    return False


def _fetch_chatgpt_session(driver, timeout: int = 90, auto_jump_wait: int = 15) -> dict:
    """等待页面完成跳转并从 ChatGPT 页面内读取登录 session/accessToken。

    旧逻辑会在 auth.openai.com 上一直等到总超时，Cloak/部分 Chromium 场景下
    实际账号已创建成功但当前句柄 URL 没及时更新，导致白等 120 秒。现在只给
    自动跳转 `auto_jump_wait` 秒；超过后立即主动打开 chatgpt.com 读 session。
    """
    end = time.time() + timeout
    auto_jump_end = time.time() + max(3, int(auto_jump_wait or 15))
    last_data = None
    forced_chatgpt_open = False
    cf_done = False

    while time.time() < end:
        try:
            current = str(driver.current_url or '')
        except Exception:
            current = ''
        # OAuth 回调落到 /auth/error：这是错误节点，别空转到超时，立刻抛出让恢复链接手。
        if "auth/error" in current.lower():
            raise RuntimeError(f"OAuth 回调落在 auth/error 页，提前触发恢复: {current[:180]}")
        # 跳转后可能落在 Cloudflare 整页挑战上；先解决再读 session。
        active, cf_done, end = _cf_watch_tick(driver, end=end, label="读取 session", done=cf_done)
        if active:
            _fsleep(1)
            continue

        if 'chatgpt.com' not in current:
            if _switch_to_chatgpt_window_if_any(driver):
                current = str(getattr(driver, "current_url", "") or "")
            elif time.time() >= auto_jump_end and not forced_chatgpt_open:
                try:
                    logger.info("%s 未在 %ss 内观察到当前窗口跳转 chatgpt.com，主动打开 ChatGPT 内读取 session", _log_prefix(driver), int(auto_jump_wait or 15))
                    _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
                    forced_chatgpt_open = True
                    _fsleep(3)
                    current = str(getattr(driver, "current_url", "") or "")
                except Exception as exc:
                    last_data = f"{type(exc).__name__}: {exc}"
            else:
                _fsleep(1)
                continue

        if 'chatgpt.com' in current:
            try:
                data = _read_chatgpt_session_once(driver)
                if data:
                    return data
                last_data = "session 暂无 accessToken"
            except Exception as exc:
                last_data = f"{type(exc).__name__}: {exc}"
        _fsleep(2)

    raise RuntimeError(f"等待 /api/auth/session accessToken 超时，最后响应: {str(last_data)[:800]}")


def _enable_2fa_in_roxy(driver, email: str) -> tuple[str, str] | None:
    """在 Roxy 浏览器内完成 2FA（TOTP）设置。

    全程复用当前浏览器的会话 cookie / 出口 IP / UA（Selenium + 浏览器内 fetch），
    避免纯协议新起会话导致 IP/指纹不一致被风控。返回 (totp_secret, new_access_token)；
    任一步骤失败返回 None（2FA 是可选增强，失败不影响注册成功）。
    """
    import pyotp

    def _safe_nav(url: str, hosts: tuple[str, ...]) -> None:
        try:
            # script_timeout=90：2FA 的浏览器内 fetch 长链路依赖放宽的 script timeout，
            # 不能用默认 8s（否则每次跳转都会把它压回去，导致 execute_async_script 超时）。
            _safe_get(driver, url, timeout=45, attempts=2, accept_hosts=hosts, script_timeout=90)
        except Exception:
            pass

    try:
        # 2FA 的浏览器内 fetch 链路较长（csrf→signin→OTP→enroll→activate），
        # 注册主流程把 script timeout 压到了 12s，这里临时放宽，结束后恢复。
        saved_script_timeout = None
        try:
            saved_script_timeout = driver.get_script_timeout()
        except Exception:
            saved_script_timeout = None
        try:
            driver.set_script_timeout(90)
        except Exception:
            pass

        # 0) 确保在 chatgpt.com 登录态
        _safe_nav("https://chatgpt.com/", ("chatgpt.com",))
        human_delay("navigate")

        # 1) 浏览器内触发密码重认证，拿 auth.openai.com authorize URL
        auth_url = driver.execute_async_script(r"""
            const email = String(arguments[0] || '').trim();
            const done = arguments[arguments.length - 1];
            const ac = new AbortController();
            const to = setTimeout(() => ac.abort(), 30000);
            (async () => {
              try {
                const csrf = await fetch('/api/auth/csrf', {credentials:'include', signal: ac.signal})
                  .then(r => r.json()).then(d => d.csrfToken);
                const q = new URLSearchParams({connection:'password', login_hint:email, reauth:'password', max_age:'0'});
                const res = await fetch('/api/auth/signin/openai?' + q, {
                  method:'POST', credentials:'include', signal: ac.signal,
                  headers:{'content-type':'application/x-www-form-urlencoded'},
                  body: new URLSearchParams({callbackUrl:'https://chatgpt.com/?action=enable&factor=totp', csrfToken:csrf, json:'true'})
                });
                const data = await res.json();
                done(data.url || '');
              } catch (e) { done('ERR:' + (e && e.message)); }
              finally { clearTimeout(to); }
            })();
        """, email)
        if not auth_url or str(auth_url).startswith("ERR:"):
            logger.warning("%s[2FA] 触发重认证失败：%s", _log_prefix(driver), auth_url)
            return None
        logger.info("%s[2FA] 触发重认证成功，导航 authorize URL", _log_prefix(driver))

        # 2) 导航 authorize URL → auth.openai.com，触发新的邮箱 OTP
        otp_after_ts = time.time()
        _safe_nav(str(auth_url), ("auth.openai.com", "chatgpt.com"))
        human_delay("navigate")
        # auth.openai.com 重认证页也可能触发 Cloudflare/Turnstile，先自动解决再等 OTP。
        _solve_cf(driver, timeout=20, label="2FA 重认证页")
        otp = wait_for_otp(email, after_ts=otp_after_ts)
        logger.info("%s[2FA] 收到重认证邮箱验证码", _log_prefix(driver))

        # 3) 浏览器已在 auth.openai.com，同源提交 OTP 拿到 continue_url
        continue_url = driver.execute_async_script(r"""
            const code = String(arguments[0] || '');
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const res = await fetch('/api/accounts/email-otp/validate', {
                  method:'POST', credentials:'include',
                  headers:{'content-type':'application/json'},
                  body: JSON.stringify({code: code})
                });
                const data = await res.json();
                done(data.continue_url || '');
              } catch (e) { done('ERR:' + (e && e.message)); }
            })();
        """, otp)
        if not continue_url or str(continue_url).startswith("ERR:"):
            logger.warning("%s[2FA] 重认证 OTP 验证失败：%s", _log_prefix(driver), continue_url)
            return None
        human_delay("form")

        # 4) 跟随 continue_url 回 chatgpt.com，刷新 session token
        _safe_nav(str(continue_url), ("auth.openai.com", "chatgpt.com"))
        _safe_nav("https://chatgpt.com/", ("chatgpt.com",))
        _solve_cf(driver, timeout=20, label="2FA 回跳页")
        new_token = driver.execute_async_script(r"""
            const done = arguments[arguments.length - 1];
            fetch('/api/auth/session', {credentials:'include'})
              .then(r => r.json()).then(d => done(d && d.accessToken || ''))
              .catch(() => done(''));
        """)
        if not new_token:
            logger.warning("%s[2FA] 重认证后未拿到新 accessToken", _log_prefix(driver))
            return None
        logger.info("%s[2FA] 已拿到重认证后新 accessToken", _log_prefix(driver))

        # 5) enroll TOTP（浏览器内同源请求）
        enroll = driver.execute_async_script(r"""
            const token = String(arguments[0] || '');
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const res = await fetch('/backend-api/accounts/mfa/enroll', {
                  method:'POST', credentials:'include',
                  headers:{'content-type':'application/json', 'authorization':'Bearer ' + token},
                  body: JSON.stringify({factor_type:'totp'})
                });
                const data = await res.json();
                done({secret: data.secret || '', session_id: data.session_id || '', err: ''});
              } catch (e) { done({secret:'', session_id:'', err:String(e && e.message)}); }
            })();
        """, new_token)
        secret = str((enroll or {}).get("secret") or "")
        session_id = str((enroll or {}).get("session_id") or "")
        if not secret or not session_id:
            logger.warning("%s[2FA] enroll TOTP 失败：%s", _log_prefix(driver), (enroll or {}).get("err") or enroll)
            return None

        # 6) 用 secret 生成 TOTP 码并激活
        totp_code = pyotp.TOTP(secret).now()
        activate = driver.execute_async_script(r"""
            const token = String(arguments[0] || '');
            const code = String(arguments[1] || '');
            const sid = String(arguments[2] || '');
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const res = await fetch('/backend-api/accounts/mfa/user/activate_enrollment', {
                  method:'POST', credentials:'include',
                  headers:{'content-type':'application/json', 'authorization':'Bearer ' + token},
                  body: JSON.stringify({code: code, factor_type:'totp', session_id: sid})
                });
                done({ok: res.ok, status: res.status});
              } catch (e) { done({ok:false, err:String(e && e.message)}); }
            })();
        """, new_token, totp_code, session_id)
        if not (activate or {}).get("ok"):
            logger.warning("%s[2FA] activate TOTP 失败：%s", _log_prefix(driver), activate)
            return None

        logger.info("%s[2FA] TOTP 激活成功，secret=%s...%s", _log_prefix(driver), secret[:4], secret[-4:])
        return secret, new_token
    except Exception as exc:
        logger.warning("%s[2FA] 设置失败，已跳过（不影响注册成功）：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:200])
        return None
    finally:
        if saved_script_timeout is not None:
            try:
                driver.set_script_timeout(saved_script_timeout)
            except Exception:
                pass


def _enable_2fa_with_retry(driver, email: str, max_attempts: int | None = None) -> tuple[str, str] | None:
    """设置 2FA（TOTP），失败自动重试多次，而不是一次失败就结束。

    每次尝试都是完整流程（重新触发密码重认证 → 新 OTP → enroll TOTP → activate）。
    单次 OTP 偶发收不到 / 校验过期 / 激活竞态时，重试能显著提高成功率；连续失败才放弃
    （2FA 是可选增强，失败不影响注册成功）。返回 (totp_secret, new_access_token)。
    """
    if max_attempts is None:
        try:
            from config import twofa as _twofa_cfg
            max_attempts = int(getattr(_twofa_cfg, "TWOFA_MAX_ATTEMPTS", 3) or 3)
        except Exception:
            max_attempts = 3
    max_attempts = max(1, max_attempts)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        logger.info("%s[2FA] 开始设置 TOTP（第 %s/%s 次）", _log_prefix(driver), attempt, max_attempts)
        try:
            result = _enable_2fa_in_roxy(driver, email)
        except Exception as exc:
            result = None
            last_err = f"{type(exc).__name__}: {str(exc)[:180]}"
        if result:
            if attempt > 1:
                logger.info("%s[2FA] 第 %s 次重试后设置成功", _log_prefix(driver), attempt)
            return result
        if attempt < max_attempts:
            last_err = last_err or "详情见上方日志"
            delay = random.uniform(2.5, 5.0)
            logger.warning(
                "%s[2FA] 第 %s/%s 次设置失败，%.1f 秒后重试：%s",
                _log_prefix(driver), attempt, max_attempts, delay, last_err,
            )
            _fsleep(delay)
    logger.warning("%s[2FA] 连续 %s 次设置失败，跳过（不影响注册成功）：%s", _log_prefix(driver), max_attempts, last_err)
    return None


def _read_oai_session_payload(driver) -> dict | None:
    """当前页面必须在 auth.openai.com 域（oai-client-auth-session 是 .openai.com 域 cookie）。
    读取会话 cookie 第一个段（base64url JSON payload）。读不到返回 None。"""
    script = r"""
    const done = arguments[arguments.length - 1];
    try {
        const c = document.cookie.split(';').map(s => s.trim()).find(s => s.startsWith('oai-client-auth-session='));
        if (!c) return done(null);
        const raw = c.split('=').slice(1).join('=');
        const seg = raw.split('.')[0];
        const pad = s => s + '='.repeat((4 - s.length % 4) % 4);
        const json = JSON.parse(atob(pad(seg).replace(/-/g, '+').replace(/_/g, '/')));
        return done(json);
    } catch (e) { return done({_err: String(e)}); }
    """
    try:
        return driver.execute_async_script(script)
    except Exception:
        return None


def _add_password_post_signup(driver, email: str, password: str) -> bool:
    """signup 会话下补设 ChatGPT 登录密码（浏览器内完整 reset 流程）。

    新邮箱注册直达 OTP 页（服务端 passwordless_primary，signup 无 password 页），
    会话也始终无 post_login_add_password 标志，/api/accounts/password/add 恒 409——
    已由多次实弹探针证实。能设密码的可靠路径是官方 reset 协议：
      A. 直接同源 POST /api/accounts/password/reset（body 仅 {password}，凭证靠会话 cookie）
      B. 驱动 /reset-password/new-password 页真实表单（两个密码框 + 继续按钮）
      C. 完整邮件链接流：reset start 页点 intent=send_otp → 收重置邮件 →
         导航链接（带重置 cookie）→ new-password 表单提交
    三条路依次尝试，全部复用当前会话/出口 IP/UA。返回是否设置成功；失败仅告警，
    不影响注册成功。所有 /password/reset、/password/send-otp 的 HTTP 响应都会被
    fetch 拦截器捕获落日志，供实弹校准确认真实 schema。
    """
    saved_script_timeout = None
    try:
        try:
            saved_script_timeout = driver.get_script_timeout()
        except Exception:
            saved_script_timeout = None
        try:
            driver.set_script_timeout(90)
        except Exception:
            pass

        # 1) 导航到 auth.openai.com 域（读 .openai.com 会话 cookie + 同源调 API）。
        #    先落在 new-password 页：无邮件重置 cookie 时该页可能渲染"修改密码"表单或
        #    "会话已结束"，都不阻断后续尝试。
        _safe_get(driver, "https://auth.openai.com/reset-password/new-password",
                  timeout=40, attempts=2, accept_hosts=("auth.openai.com", "chatgpt.com"),
                  script_timeout=90)
        human_delay("navigate")

        payload = _read_oai_session_payload(driver)
        flag = bool((payload or {}).get("post_login_add_password"))
        logger.info("%s[密码] 会话 post_login_add_password=%s username=%s（仅日志，不阻塞尝试）",
                    _log_prefix(driver), flag, str((payload or {}).get("email") or (payload or {}).get("username"))[:40])

        # 2) 注入 fetch 拦截器：记录 /password/reset 与 /password/send-otp 的请求/响应。
        _inject_pwd_fetch_capture(driver)

        # 3) 路径 A：直接同源 POST /api/accounts/password/reset。
        res = driver.execute_async_script(r"""
            const pwd = String(arguments[0] || '');
            const done = arguments[arguments.length - 1];
            (async () => {
                try {
                    const res = await fetch('/api/accounts/password/reset', {
                        method: 'POST', credentials: 'include',
                        headers: {'content-type': 'application/json'},
                        body: JSON.stringify({password: pwd})
                    });
                    const text = await res.text();
                    return done({status: res.status, text: text.slice(0, 400)});
                } catch (e) { return done({status: 0, err: String(e && e.message)}); }
            })();
        """, password)
        status = int((res or {}).get("status") or 0)
        if 200 <= status < 300:
            logger.info("%s[密码] 路径A 直接 /password/reset 设置成功 HTTP %s", _log_prefix(driver), status)
            return True
        logger.warning("%s[密码] 路径A 直接 /password/reset 未成功 HTTP %s：%s",
                       _log_prefix(driver), status, str(res or {})[:180])

        # 4) 路径 B：驱动 new-password 页真实表单。
        if _submit_new_password_form(driver, password):
            return True

        # 5) 路径 C：完整邮件链接流（最可靠）。
        if _submit_password_via_reset_email(driver, email, password):
            return True

        logger.warning("%s[密码] 三条补密码路径均未成功（路径A/B/C）", _log_prefix(driver))
        return False
    except Exception as exc:
        logger.warning("%s[密码] signup 补密码失败（不影响注册成功）：%s: %s",
                       _log_prefix(driver), type(exc).__name__, str(exc)[:200])
        return False
    finally:
        if saved_script_timeout is not None:
            try:
                driver.set_script_timeout(saved_script_timeout)
            except Exception:
                pass


def _inject_pwd_fetch_capture(driver) -> None:
    """注入 fetch/XHR 拦截器，把 /password/reset 与 /password/send-otp 的响应存进 window.__pwdResp。"""
    try:
        driver.execute_script(r"""
            if (window.__pwdResp) return;
            window.__pwdResp = [];
            const grab = async (url, init) => {
                if (!/(password\/reset|password\/send-otp)/.test(url)) return;
                try {
                    const res = await fetch(url, init);
                    const text = await res.text().catch(() => '');
                    window.__pwdResp.push({url, status: res.status, text: text.slice(0, 300)});
                } catch (e) { window.__pwdResp.push({url, status: 0, text: String(e && e.message)}); }
            };
            const origFetch = window.fetch;
            window.fetch = async (input, init) => {
                const url = (typeof input === 'string') ? input : (input && input.url) || '';
                if (/(password\/reset|password\/send-otp)/.test(url)) {
                    grab(url, init);
                }
                return origFetch.apply(this, arguments);
            };
        """)
    except Exception:
        pass


def _dump_pwd_capture(driver, label: str) -> None:
    """把拦截到的 password 相关响应落日志（实弹校准用）。"""
    try:
        captured = driver.execute_script("return window.__pwdResp || []") or []
        if captured:
            for c in captured[-5:]:
                logger.info("%s[密码][抓包] %s %s HTTP %s body=%s", _log_prefix(driver), label, c.get("url"), c.get("status"), str(c.get("text") or "")[:200])
    except Exception:
        pass


def _submit_new_password_form(driver, password: str) -> bool:
    """路径 B：驱动 new-password 页的两个密码输入框 + 提交按钮，等待跳转/成功提示。"""
    filled = driver.execute_async_script(r"""
        const pwd = String(arguments[0] || '');
        const done = arguments[arguments.length - 1];
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
            && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"],input[autocomplete="current-password"]')]
            .filter(visible).slice(0, 4);
        if (inputs.length < 2) return {ok: false, reason: 'need_two_password_inputs', count: inputs.length};
        inputs[0].value = pwd;
        inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
        inputs[0].dispatchEvent(new Event('change', {bubbles: true}));
        inputs[1].value = pwd;
        inputs[1].dispatchEvent(new Event('input', {bubbles: true}));
        inputs[1].dispatchEvent(new Event('change', {bubbles: true}));
        return {ok: true, count: inputs.length};
    """, password)
    if not (filled or {}).get("ok"):
        try:
            _diag_url = str(driver.current_url or '')
            _diag_text = str(driver.execute_script("return document.body ? document.body.innerText : ''") or '')
            _diag_inputs = driver.execute_script(r"""
                return [...document.querySelectorAll('input')].map(el => ({
                  type: el.type||'', name: el.name||'', ac: el.getAttribute('autocomplete')||''
                }));
            """)
            logger.warning("%s[密码][路径B] 表单输入框不足（url=%s）inputs=%s，页面文本前300：%s",
                           _log_prefix(driver), _diag_url, str(_diag_inputs)[:200], _diag_text[:300].replace("\n", " | "))
        except Exception:
            pass
        return False
    clicked = driver.execute_async_script(r"""
        const done = arguments[arguments.length - 1];
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
            && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const norm = s => (s || '').toLowerCase().replace(/\s+/g, '');
        const textBtn = [...document.querySelectorAll('button,input[type="submit"]')]
            .find(el => visible(el) && /(continue|保存|登録|設定|submit|完了|次へ|続行|完成|add|reset)/.test(norm(el.textContent || el.value || '')));
        if (textBtn) { textBtn.click(); return done({ok: true, via: 'text'}); }
        const anyBtn = [...document.querySelectorAll('button,input[type="submit"]')].filter(visible).pop();
        if (anyBtn) { anyBtn.click(); return done({ok: true, via: 'any'}); }
        return done({ok: false, reason: 'no_submit_button'});
    """)
    if not (clicked or {}).get("ok"):
        logger.warning("%s[密码][路径B] 表单未找到提交按钮：%s", _log_prefix(driver), clicked)
        return False
    human_delay("form")
    end = time.time() + 25
    cf_done = False
    ok = False
    while time.time() < end:
        active, cf_done, end = _cf_watch_tick(driver, end=end, label="补密码提交B", done=cf_done)
        if active:
            _fsleep(1)
            continue
        try:
            url = str(driver.current_url or '')
        except Exception:
            url = ''
        if 'new-password' not in url:
            ok = True
            break
        _fsleep(1)
    _dump_pwd_capture(driver, "[路径B]")
    if ok:
        logger.info("%s[密码][路径B] 表单提交后已跳转：%s", _log_prefix(driver), url)
        return True
    try:
        body = str(driver.execute_script("return document.body ? document.body.innerText : ''") or '')
        if any(k in body for k in ('已设置', 'パスワードを更新', 'password has been', 'Password updated', '成功', 'パスワードを設定')):
            logger.info("%s[密码][路径B] new-password 页出现成功提示", _log_prefix(driver))
            return True
    except Exception:
        pass
    logger.warning("%s[密码][路径B] 表单提交后未检测到跳转/成功提示", _log_prefix(driver))
    return False


def _submit_password_via_reset_email(driver, email: str, password: str) -> bool:
    """路径 C：完整邮件链接流。reset start 页点 intent=send_otp → 重置邮件 →
    导航链接（带重置 cookie）→ new-password 表单提交。最可靠，不依赖任何会话标志。
    """
    # 5.1 导航到 reset start 页，找 intent=send_otp 提交按钮。
    _safe_get(driver, "https://auth.openai.com/reset-password",
              timeout=40, attempts=2, accept_hosts=("auth.openai.com", "chatgpt.com"),
              script_timeout=90)
    human_delay("navigate")
    start = driver.execute_async_script(r"""
        const done = arguments[arguments.length - 1];
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const norm = s => (s || '').toLowerCase().replace(/\s+/g, '');
        const byAttr = [...document.querySelectorAll('button,input[type="submit"]')]
            .find(el => visible(el) && el.getAttribute('name') === 'intent' && el.getAttribute('value') === 'send_otp');
        if (byAttr) { byAttr.click(); return done({ok: true, via: 'attr'}); }
        const byText = [...document.querySelectorAll('button,input[type="submit"]')]
            .find(el => visible(el) && /(reset password|パスワードをリセット|パスワードを再設定|重置密码)/.test(norm(el.textContent || el.value || '')));
        if (byText) { byText.click(); return done({ok: true, via: 'text'}); }
        const body = (document.body ? document.body.innerText : '').slice(0, 300);
        return done({ok: false, reason: 'no_send_otp_button', body: body});
    """)
    if not (start or {}).get("ok"):
        logger.warning("%s[密码][路径C] reset start 页未找到 send_otp 按钮：%s", _log_prefix(driver), str(start or {})[:220])
        return False
    logger.info("%s[密码][路径C] 已点击 send_otp（%s），等待重置邮件", _log_prefix(driver), start.get("via"))
    human_delay("api")

    # 5.2 等重置邮件 → 取链接。
    try:
        from core.email_provider import wait_for_reset_link
        reset_url = wait_for_reset_link(email, after_ts=time.time(), max_wait=120)
    except Exception as exc:
        _dump_pwd_capture(driver, "[路径C]")
        logger.warning("%s[密码][路径C] 未取到重置链接（不影响注册成功）：%s", _log_prefix(driver), str(exc)[:160])
        return False
    _dump_pwd_capture(driver, "[路径C]")

    # 5.3 导航到重置链接（服务端 set 重置态 cookie），落地 new-password 页。
    _safe_get(driver, reset_url, timeout=50, attempts=2, accept_hosts=("auth.openai.com", "chatgpt.com"),
              script_timeout=90)
    human_delay("navigate")

    # 5.4 驱动 new-password 表单。
    if _submit_new_password_form(driver, password):
        logger.info("%s[密码][路径C] 重置链接流表单提交成功", _log_prefix(driver))
        return True
    logger.warning("%s[密码][路径C] 重置链接流表单提交未成功", _log_prefix(driver))
    return False


def _check_manual_stop() -> None:
    try:
        from core.registration_service import check_stop_requested, run_deadline_reached
        check_stop_requested()
        if run_deadline_reached():
            # 单次 run 超过 RUN_MAX_MINUTES：代理 -t-5 粘性 5 分钟会换 IP，
            # 此前积累的 cf_clearance/会话随之失效，继续硬跑只会反复撞 CF。
            # 抛错由 job 重试机制换新 sid 重跑。
            raise RuntimeError(
                "注册超时（RUN_MAX_MINUTES 分钟），为避免代理粘性换 IP 使 cf_clearance 失效，终止本次注册"
            )
    except ImportError:
        return


def _registration_recovery_abort(exc: BaseException) -> bool:
    """手动停止/单次运行截止时间不进入页面恢复链。"""
    text = str(exc or "")
    return (
        type(exc).__name__ == "StopRequested"
        or "手动停止" in text
        or "注册超时（RUN_MAX_MINUTES" in text
    )


def _registration_stage_state(driver) -> str:
    """恢复前只读探测当前注册节点，不触发任何表单动作。"""
    terminal = _terminal_registration_state(driver)
    if terminal:
        return terminal
    snapshot = _page_snapshot(driver)
    if _probe_state_unsettled(snapshot):
        return "unknown"
    if _is_profile_like(snapshot):
        return "profile"
    url = str(snapshot.get("url") or getattr(driver, "current_url", "") or "").lower()
    # 报错页：chatgpt.com/auth/error? 等。识别为独立节点，恢复时重新进对应阶段入口。
    if "auth/error" in url:
        return "auth_error"
    if "chatgpt.com" in url and "/auth/" not in url:
        return "chatgpt"
    if _is_email_login_page_still_present(driver):
        return "email"
    return "unknown"


def _registration_recovery_actions(
    *,
    stage_url: str | None,
    previous_url: str | None,
    replay_previous=None,
    reauthenticate=None,
) -> list[str]:
    actions = ["reprobe", "refresh"]
    if str(stage_url or "").strip():
        actions.append("reenter_stage")
    # 会话丢失/异常跳转到登录页或报错页时，重新走登录入口恢复当前节点。
    if reauthenticate is not None:
        actions.append("reauthenticate")
    if str(previous_url or "").strip() and replay_previous is not None:
        actions.append("return_previous")
    return actions


def _apply_registration_recovery(
    driver,
    stage: str,
    action: str,
    *,
    stage_url: str | None = None,
    previous_url: str | None = None,
    replay_previous=None,
    reauthenticate=None,
) -> dict:
    """执行单个恢复动作并返回只读状态快照；动作失败也交给下一层继续。"""
    _check_manual_stop()
    action_error = None
    try:
        if action == "refresh":
            driver.refresh()
        elif action == "reenter_stage":
            _safe_get(
                driver,
                str(stage_url),
                timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                attempts=1,
            )
        elif action == "reauthenticate":
            # 重新走登录入口恢复会话/节点（reauthenticate 回调负责具体重放）。
            if reauthenticate is not None:
                _maybe_accept(driver)
                reauthenticate()
        elif action == "return_previous":
            # 先使用浏览器历史返回；再以记录的 URL 校准，避免 refresh/get 改写历史栈。
            try:
                driver.back()
            except Exception as exc:
                logger.info("%s[恢复][%s] history.back 未完成，改用上一步 URL：%s", _log_prefix(driver), stage, str(exc)[:160])
            _safe_get(
                driver,
                str(previous_url),
                timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                attempts=1,
            )
            replay_previous()
        elif action != "reprobe":
            raise ValueError(f"未知恢复动作: {action}")

        if action != "reprobe":
            _maybe_accept(driver)
    except Exception as exc:
        if _registration_recovery_abort(exc):
            raise
        action_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        logger.warning(
            "%s[恢复][%s] 动作 %s 未完成，继续重探测：%s",
            _log_prefix(driver), stage, action, action_error,
        )

    _check_manual_stop()
    try:
        state = _registration_stage_state(driver)
    except Exception as exc:
        if _registration_recovery_abort(exc):
            raise
        state = "unknown"
        if action_error is None:
            action_error = f"state_probe={type(exc).__name__}: {str(exc)[:220]}"
    report = {
        "action": action,
        "state": state,
        "url": str(getattr(driver, "current_url", "") or "")[:300],
    }
    if action_error:
        report["error"] = action_error
    logger.info("%s[恢复][%s] %s", _log_prefix(driver), stage, report)
    return report


def _run_stage_with_recovery(
    driver,
    stage: str,
    operation,
    *,
    stage_url: str | None = None,
    previous_url: str | None = None,
    replay_previous=None,
    resume_from_state=None,
    reauthenticate=None,
):
    """运行一个注册阶段；穷尽有限恢复动作后才向上抛出失败。

    ``resume_from_state`` 返回 ``(True, result)`` 时表示只读探测已确认阶段完成，
    因而不重放表单。OTP 提交后的等待可借此确认已进入资料页/登录态，避免重复提交。

    ``reauthenticate`` 提供会话丢失（跳回登录页 / auth/error）时重新走登录入口
    恢复当前节点的回调，是异常恢复的最后一道。
    """
    actions = _registration_recovery_actions(
        stage_url=stage_url,
        previous_url=previous_url,
        replay_previous=replay_previous,
        reauthenticate=reauthenticate,
    )
    history: list[dict] = []
    last_exc: BaseException | None = None

    for attempt in range(len(actions) + 1):
        _check_manual_stop()
        try:
            return operation()
        except Exception as exc:
            if _registration_recovery_abort(exc):
                raise
            # operation 运行期间可能刚刚越过总截止时间。
            _check_manual_stop()
            last_exc = exc
            history.append({
                "attempt": attempt + 1,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
            if attempt >= len(actions):
                break

        report = _apply_registration_recovery(
            driver,
            stage,
            actions[attempt],
            stage_url=stage_url,
            previous_url=previous_url,
            replay_previous=replay_previous,
            reauthenticate=reauthenticate,
        )
        history.append(report)
        if resume_from_state is not None:
            done, result = resume_from_state(report.get("state"), report)
            if done:
                logger.info(
                    "%s[恢复][%s] 状态重探测确认已推进，跳过动作重放：state=%s",
                    _log_prefix(driver), stage, report.get("state"),
                )
                return result

    final_state = "unknown"
    final_url = str(getattr(driver, "current_url", "") or "")[:300]
    try:
        final_state = _registration_stage_state(driver)
    except Exception:
        pass
    detail = f"state={final_state} url={final_url} history={str(history)[-1800:]}"
    raise RuntimeError(
        f"{stage}失败，已穷尽当前阶段可用的页面重探测/刷新/节点重进/上一步恢复策略；{detail}；"
        f"last={type(last_exc).__name__ if last_exc else 'RuntimeError'}: {str(last_exc)[:400] if last_exc else '-'}"
    ) from last_exc


def _resend_email_otp_with_recovery(driver, email: str, *, entry_url: str = "https://chatgpt.com/auth/login"):
    """把重新发送验证码也放进恢复链，避免按钮偶发失效时立即结束注册。"""
    stage_url = str(getattr(driver, "current_url", "") or "").strip() or entry_url

    def replay_email_entry() -> None:
        _maybe_accept(driver)
        _submit_email_and_wait_next(driver, email, attempts=2)

    return _run_stage_with_recovery(
        driver,
        "重新发送邮箱验证码",
        lambda: _click_resend_email_otp(driver, timeout=25),
        stage_url=stage_url,
        previous_url=entry_url,
        replay_previous=replay_email_entry,
    )


def _recover_login_flow(driver, *, email: str, name: str = "", birthday: str = "", auth_entry_url: str = "https://chatgpt.com/auth/login") -> None:
    """注册中途会话被重置（跳到 /auth/error 或回登录页）后的兜底恢复。

    重新走「登录入口 → 提交邮箱 → (OTP) → 资料页」把浏览器拉回当前注册节点。
    已注册账号会进密码/登录路径，由 _submit_email_and_wait_next 返回状态自然推进；
    恢复失败或账号已注册时，外层 _run_stage_with_recovery 仍会按原有策略兜底。
    """
    _maybe_accept(driver)
    _safe_get(
        driver,
        auth_entry_url,
        timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    human_delay("navigate")
    _page_warmup(driver, reason="recovery_relogin")
    try:
        state = _submit_email_and_wait_next(driver, email, attempts=2)
    except Exception as exc:
        if _is_transient_navigation_error(exc, driver):
            state = _registration_stage_state(driver)
        else:
            raise
    if state == "otp":
        # 重新发码后拿新验证码并提交，推进到资料页/登录态
        new_otp = wait_for_otp(email, after_ts=time.time())
        _clear_otp_inputs(driver)
        _type_otp(driver, new_otp)
        try:
            _click_continue(driver)
        except Exception:
            pass
        _wait_after_email_otp_submit(driver, timeout=_CF_TRANSITION_TIMEOUT)
    if name:
        _complete_profile_page(driver, name, birthday, timeout=45)


_ROXY_REGISTRATION_IP_MAX_ATTEMPTS = 5
_ROXY_PROXY_MAX_ATTEMPTS = 4
_REGISTRATION_IP_RESERVATION_LOCK = threading.RLock()
_REGISTRATION_IP_RESERVATIONS: dict[str, int] = {}


_PROXY_NETWORK_ERROR_HINTS = (
    "proxy", "socks", "tunnel", "err_proxy", "err_tunnel",
    "timeout", "timed out", "connection", "connect to chrome",
    "connection refused", "connection reset", "connection closed",
    "name resolution", "dns", "net::err_", "renderer",
    "http 403", "http 408", "http 425", "http 429",
    "http 500", "http 502", "http 503", "http 504",
)
_TERMINAL_REGISTRATION_ERROR_HINTS = (
    "验证码错误", "验证码过期", "invalid otp", "incorrect otp",
    "invalid password", "wrong password", "密码错误",
    "already registered", "invalid email", "account disabled",
    "account deactivated", "账号停用", "账号禁用",
    "manual stop", "手动停止", "stoprequested",
)


def _is_proxy_network_error(exc: BaseException) -> bool:
    """Classify only route/transport failures for cross-proxy retry.

    Page/business failures must stay inside the existing stage recovery and
    OTP/account logic.  A terminal business hint wins even if its message also
    happens to contain a generic word such as ``timeout``.
    """
    if exc is None:
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in text for hint in _TERMINAL_REGISTRATION_ERROR_HINTS):
        return False
    return any(hint in text for hint in _PROXY_NETWORK_ERROR_HINTS)


def _set_registration_deadline() -> None:
    try:
        from core.registration_service import _run_max_minutes, set_run_deadline
        set_run_deadline(_run_max_minutes())
    except Exception:
        pass


def _claim_registration_ip(
    registration_ip: str,
    *,
    accept_duplicate: bool,
) -> tuple[bool, int, int]:
    """Atomically check saved/in-flight use and optionally reserve this IP."""
    from core import db as _db

    with _REGISTRATION_IP_RESERVATION_LOCK:
        history_count = _db.account_registration_ip_count(registration_ip)
        in_flight_count = int(_REGISTRATION_IP_RESERVATIONS.get(registration_ip, 0) or 0)
        duplicate = history_count > 0 or in_flight_count > 0
        if duplicate and not accept_duplicate:
            return False, history_count, in_flight_count
        _REGISTRATION_IP_RESERVATIONS[registration_ip] = in_flight_count + 1
        return True, history_count, in_flight_count


def _release_registration_ip(registration_ip: str) -> None:
    if not registration_ip:
        return
    with _REGISTRATION_IP_RESERVATION_LOCK:
        count = int(_REGISTRATION_IP_RESERVATIONS.get(registration_ip, 0) or 0)
        if count <= 1:
            _REGISTRATION_IP_RESERVATIONS.pop(registration_ip, None)
        else:
            _REGISTRATION_IP_RESERVATIONS[registration_ip] = count - 1


def _discard_roxy_preflight_attempt(client, opened, driver) -> None:
    """Force-close a duplicate-IP temporary environment before retrying."""
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    if opened is None:
        return
    try:
        client.close_profile(opened.profile_id)
    except Exception:
        pass
    if bool(getattr(opened, "created_by_run", False)):
        try:
            client.delete_profile(opened.profile_id)
        except Exception:
            pass


def _prepare_roxy_registration_browser(client, registration_country: str = ""):
    """Open, probe and optionally rotate Roxy environments before signup."""
    avoid_duplicates = bool(getattr(_cfg, "ROXY_AVOID_DUPLICATE_REGISTRATION_IP", True))
    # Duplicate-IP rotation has its own previously requested five-environment
    # rule.  Proxy/network failures have a separate four-attempt budget and do
    # not consume the duplicate-IP counter.
    registration_country = normalize_country_code(
        registration_country,
        strict=bool(str(registration_country or "").strip()),
    )
    ip_max_attempts = _ROXY_REGISTRATION_IP_MAX_ATTEMPTS if avoid_duplicates else 1
    ip_attempt = 0
    proxy_attempt = 0

    while ip_attempt < ip_max_attempts and proxy_attempt < _ROXY_PROXY_MAX_ATTEMPTS:
        opened = None
        driver = None
        try:
            opened = client.open_profile()
            driver = _build_driver(opened)
            _center_browser_window(driver)
            driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
            try:
                driver.set_script_timeout(12)
            except Exception:
                pass
            if registration_country:
                registration_geo = detect_selenium_registration_geo(
                    driver,
                    log_prefix="[Roxy注册]",
                    require_country=True,
                )
            else:
                # Keep the established IP-only seam for integrations/tests;
                # country-aware jobs use the richer detector above.
                registration_geo = {
                    "ip": detect_selenium_registration_ip(driver, log_prefix="[Roxy注册]"),
                    "country": "",
                }
            route_ok, route_error = registration_geo_matches(
                registration_geo,
                registration_country,
            )
            if not route_ok:
                raise RuntimeError(route_error)
            registration_ip = registration_geo["ip"]
            logger.info(
                "[Roxy注册][代理预检] 第 %s/%s 次通过：IP=%s country=%s target=%s",
                proxy_attempt + 1,
                _ROXY_PROXY_MAX_ATTEMPTS,
                registration_ip,
                registration_geo.get("country") or "?",
                registration_country or "不限",
            )
        except Exception as exc:
            _discard_roxy_preflight_attempt(client, opened, driver)
            route_validation_error = "代理出口" in str(exc)
            if _is_proxy_network_error(exc) or route_validation_error:
                proxy_attempt += 1
            if (
                (_is_proxy_network_error(exc) or route_validation_error)
                and proxy_attempt < _ROXY_PROXY_MAX_ATTEMPTS
            ):
                logger.warning(
                    "[Roxy注册][代理重试] 环境创建/连接/测活失败，切换代理重试：%s/%s error=%s: %s",
                    proxy_attempt,
                    _ROXY_PROXY_MAX_ATTEMPTS,
                    type(exc).__name__,
                    str(exc)[:220],
                )
                _fsleep(0.5)
                continue
            raise

        ip_attempt += 1
        if not avoid_duplicates:
            return opened, driver, registration_ip, False
        is_last_attempt = ip_attempt >= ip_max_attempts
        claimed, history_count, in_flight_count = _claim_registration_ip(
            registration_ip,
            accept_duplicate=is_last_attempt,
        )
        if claimed:
            if history_count or in_flight_count:
                logger.warning(
                    "[Roxy注册][IP去重] 第 %s/%s 次 IP=%s 仍重复（历史=%s，并发=%s），已到第 5 次，直接继续注册",
                    ip_attempt,
                    ip_max_attempts,
                    registration_ip,
                    history_count,
                    in_flight_count,
                )
            else:
                logger.info(
                    "[Roxy注册][IP去重] 第 %s/%s 次 IP=%s 未重复，已为当前任务占用",
                    ip_attempt,
                    ip_max_attempts,
                    registration_ip,
                )
            return opened, driver, registration_ip, True

        logger.warning(
            "[Roxy注册][IP去重] 第 %s/%s 次 IP=%s 已重复（历史=%s，并发=%s），关闭环境并切换代理",
            ip_attempt,
            ip_max_attempts,
            registration_ip,
            history_count,
            in_flight_count,
        )
        _discard_roxy_preflight_attempt(client, opened, driver)
        _fsleep(0.5)

    raise RuntimeError("Roxy 注册 IP 预检未返回可用环境")


def run_roxy_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
    registration_country: str = "",
) -> dict:
    """Roxy 指纹浏览器自动化注册入口。"""
    _set_registration_deadline()
    client = RoxyBrowserClient(preferred_proxy=proxy)
    opened = None
    driver = None
    create_acknowledged = False
    openai_password: str | None = None
    registration_ip = ""
    registration_ip_reserved = False
    try:
        opened, driver, registration_ip, registration_ip_reserved = _prepare_roxy_registration_browser(
            client,
            registration_country,
        )
        # IP 轮换是正式注册前的预检；最终环境选定后重新计算注册流程时限，
        # 避免前四次换代理占掉 OTP/资料页的执行预算。
        _set_registration_deadline()
        logger.info("[Roxy注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        auth_entry_url = "https://chatgpt.com/auth/login"
        logger.info("[Roxy注册] 打开登录页：%s", auth_entry_url)
        _run_stage_with_recovery(
            driver,
            "打开注册入口",
            lambda: _safe_get(
                driver,
                auth_entry_url,
                timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                attempts=2,
                accept_hosts=("chatgpt.com", "auth.openai.com"),
            ),
            stage_url=auth_entry_url,
            resume_from_state=lambda state, _report: (state == "email", None),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="login_page")
        logger.info("[Roxy注册] 登录页加载完成，准备填写邮箱")
        _maybe_accept(driver)
        _check_manual_stop()

        # 填邮箱。OpenAI UI 会随出口 IP/语言变化；这里只按 DOM 技术属性找邮箱入口，
        # 并排除 Google/Apple/Microsoft 等第三方入口，不依赖按钮可见文字。
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

        # 新版注册流：
        #  - 邮箱直达 OTP 页（passwordless_primary）：若开启"注册时设密码"，先点 OTP 页的
        #    "使用密码继续"入口切到 /create-account/password，填密码提交后才会收到邮箱验证码；
        #    不开则纯 OTP 注册。
        #  - 邮箱直接进 /create-account/password（老流）：直接填密码。
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
                    logger.info("%s[密码] 未找到'使用密码继续'入口，走纯 OTP 注册", _log_prefix(driver))
                return None, "otp"
            return _fill_password_page_if_present(driver, email, timeout=25), None

        if advanced_state is None:
            def _password_resume(state, _report):
                # 从明确的 password 节点出发时，OTP/profile/login 都表示密码阶段已推进。
                # 从原生 OTP 节点点击“使用密码继续”失败时，OTP 仍可能是旧节点，继续重放安全动作。
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
                logger.info("[Roxy注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Roxy注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
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
            logger.info("[Roxy注册][OTP] 收到验证码：%s", current_otp)
            otp_stage_url = str(getattr(driver, "current_url", "") or "")

            def _prepare_otp_input():
                nonlocal openai_password
                state_before_otp = _registration_stage_state(driver)
                if state_before_otp in ("password", "login_password"):
                    logger.warning(
                        "%s[恢复][OTP] 验证码输入前页面迟到进入密码节点，先恢复密码步骤：state=%s",
                        _log_prefix(driver), state_before_otp,
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
                # OTP 不返回上一步：这会重新发码并使当前验证码失效。
                resume_from_state=lambda state, _report: (
                    state in ("profile", "chatgpt", "logged_in"),
                    state,
                ),
            )
            if otp_prepare_state in ("profile", "chatgpt", "logged_in"):
                advanced_state = otp_prepare_state
                break
            logger.info("[Roxy注册][OTP] 已填写邮箱验证码")
            _check_manual_stop()
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("[Roxy注册][OTP] 已提交邮箱验证码，等待资料页或登录态")
            except Exception as exc:
                logger.info("[Roxy注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _run_stage_with_recovery(
                driver,
                "等待邮箱验证码提交结果",
                lambda: _wait_after_email_otp_submit(driver, timeout=_CF_TRANSITION_TIMEOUT),
                stage_url=otp_stage_url,
                # 只等待和重探测，不会再次填写/提交同一个 OTP。
                resume_from_state=lambda state, _report: (
                    state in ("profile", "chatgpt", "logged_in"),
                    "accepted",
                ),
            )
            if outcome == 'accepted':
                break
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            logger.warning("[Roxy注册][OTP] 验证码错误/过期，准备重新发送并重新获取验证码（%s/%s）", otp_attempt + 1, max_otp_attempts)
            otp_after_ts = time.time()
            _resend_email_otp_with_recovery(driver, email, entry_url=auth_entry_url)
            human_delay("api")
            current_otp = None

        # about-you / profile 信息页：必须完成或确认已有登录态，不能静默跳过。
        logger.info("[Roxy注册] 开始等待资料页/登录态")
        _check_manual_stop()
        profile_stage_url = str(getattr(driver, "current_url", "") or "")
        profile_submitted = _run_stage_with_recovery(
            driver,
            "完成资料节点",
            lambda: _complete_profile_page(driver, name, birthday, timeout=60),
            stage_url=profile_stage_url,
            # 资料提交可幂等重放；但不返回 OTP 节点，避免重复消费验证码。
            resume_from_state=lambda state, _report: (
                state in ("chatgpt", "logged_in"),
                False,
            ),
            # 异常跳回登录页 / auth/error 时，重新走登录流恢复到资料节点。
            reauthenticate=lambda: _recover_login_flow(
                driver, email=email, name=name, birthday=birthday, auth_entry_url=auth_entry_url
            ),
        )
        if profile_submitted:
            create_acknowledged = True
            # 给 OAuth 回调 / session cookie 写入一点时间。
            human_delay("post_auth")

        logger.info("[Roxy注册] 等待 ChatGPT 跳转并写入 session/accessToken")
        _check_manual_stop()
        session_info = _run_stage_with_recovery(
            driver,
            "读取登录会话",
            lambda: _fetch_chatgpt_session(driver, timeout=120),
            stage_url="https://chatgpt.com/",
            # OAuth 回调落到 /auth/error 或回登录页时，重新走登录流恢复会话。
            reauthenticate=lambda: _recover_login_flow(
                driver, email=email, name=name, birthday=birthday, auth_entry_url=auth_entry_url
            ),
        )
        access_token = session_info["accessToken"]
        logger.info("[Roxy注册] 已拿到 accessToken：%s", email)
        _check_manual_stop()

        # 注册完成、账号已创建后：若注册时未在 password 页设过密码（新邮箱直达 OTP 页，
        # 无 password 页），则用 signup 会话的 post_login_add_password 标志在浏览器内补设密码。
        # 这是"账号从出生就带密码"的关键：既账号补密码协议被证伪，但新号会话允许设密码。
        if not openai_password and _register_set_password_enabled():
            _pwd = _registration_password()
            logger.info("[Roxy注册][密码] 注册流程未走 password 页，尝试 signup 会话补设密码（%s 位）", len(_pwd))
            if _add_password_post_signup(driver, email, _pwd):
                openai_password = _pwd
                logger.info("[Roxy注册][密码] signup 会话补设密码成功")
            else:
                logger.warning("[Roxy注册][密码] signup 会话补设密码未完成（不影响注册成功）")
            _check_manual_stop()

        if _twofa_cfg.ENABLE_2FA:
            logger.info("[Roxy注册][2FA] 启用 2FA，开始设置 TOTP（会再收一封 OTP 邮件）")
            _2fa = _enable_2fa_with_retry(driver, email)
            if _2fa:
                totp_secret, fresh_token = _2fa
                if fresh_token:
                    access_token = fresh_token
                logger.info("[Roxy注册][2FA] TOTP 设置完成，secret=%s...%s", totp_secret[:4], totp_secret[-4:])
            else:
                totp_secret = None
                logger.warning("[Roxy注册][2FA] TOTP 设置未完成，继续保存账号")
        else:
            totp_secret = None

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                # 注册流程本身已创建 Roxy 一号一环境。这里不能再新建第二个 Roxy 环境；
                # 复用当前注册窗口，先清理 Cookie/session/localStorage/cache，再开始 Codex 授权。
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Roxy注册][Codex] ENABLE_CODEX_AUTO=True，复用当前注册 Roxy 窗口执行 Codex 授权，不创建新环境")
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
                logger.info("[Roxy注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=proxy or None,
            registration_ip=registration_ip or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
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
                logger.info("[Roxy注册] 注册密码已落库 chatgpt_password 字段")
            except Exception as exc:
                logger.warning("[Roxy注册] 密码落库失败（不影响注册）: %s", str(exc)[:160])
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "password": openai_password,
            "codex": codex_result,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        logger.error("[Roxy注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Roxy注册] 失败详情", exc_info=True)
        # 未确认创建前回收邮箱；确认后避免重复使用。
        # 用 release_email_if_unconsumed：没建成账号 → 回 available 可重试；
        # 已建成账号 → 不释放，避免重复注册。
        try:
            from core.email_provider import release_email_if_unconsumed
            release_email_if_unconsumed(email, note=f"Roxy注册失败: {str(exc)[:180]}")
        except Exception:
            pass
        return {"success": False, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if driver and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
        if registration_ip_reserved:
            _release_registration_ip(registration_ip)
