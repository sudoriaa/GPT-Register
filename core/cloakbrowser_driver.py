# -*- coding: utf-8 -*-
"""CloakBrowser 的 Selenium 风格轻量适配层。"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from config import cloakbrowser as _cfg
from core.proxy_health import check_proxy_health

logger = logging.getLogger(__name__)

_CLOAK_PROXY_MAX_ATTEMPTS = 4
_CLOAK_FATAL_LAUNCH_ERROR_HINTS = (
    "license", "licence", "license_key", "invalid configuration",
    "executable doesn't exist", "executable not found", "browser not found",
)


def _is_retryable_cloak_launch_error(exc: BaseException) -> bool:
    """Retry candidate-scoped startup failures, except known global setup faults."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(hint in text for hint in _CLOAK_FATAL_LAUNCH_ERROR_HINTS):
        return False
    # An unclassified failure before registration is scoped to this candidate /
    # browser instance and is safe to retry with the next one.
    return True


def _cloak_runtime_status(configured_key: str = "") -> dict:
    """读取 Cloak 官方解析后的 key/binary 状态，不暴露 key 内容。"""
    try:
        from importlib.metadata import version as package_version
        from cloakbrowser.config import get_effective_version
        from cloakbrowser.license import resolve_license_key

        keyed = bool(resolve_license_key(configured_key or None))
        return {
            "wrapper_version": package_version("cloakbrowser"),
            "browser_version": get_effective_version(pro=keyed) or "latest-keyed",
            "keyed": keyed,
        }
    except Exception as exc:
        logger.debug("[Cloak] 读取运行时版本状态失败：%s: %s", type(exc).__name__, exc)
        return {}


@dataclass
class CloakOpenResult:
    profile_id: str = "cloakbrowser"
    raw: dict | None = None


class CloakElement:
    def __init__(self, page, locator=None, handle=None):
        self.page = page
        self.locator = locator
        self.handle = handle

    def _handle(self):
        if self.handle is not None:
            return self.handle
        return self.locator.element_handle(timeout=5000)

    def _eval(self, expression: str, arg: Any = None) -> Any:
        if self.locator is not None:
            try:
                return self.locator.evaluate(expression, arg, timeout=3000)
            except TypeError:
                return self.locator.evaluate(expression, arg)
        return self.handle.evaluate(expression, arg)

    def _eval_handle(self, expression: str, arg: Any = None) -> Any:
        h = self._handle()
        return h.evaluate_handle(expression, arg)

    def is_displayed(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_visible(timeout=800))
            return bool(self.handle.evaluate("el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'"))
        except Exception:
            return False

    def is_enabled(self) -> bool:
        try:
            if self.locator is not None:
                return bool(self.locator.is_enabled(timeout=800))
            return bool(self.handle.evaluate("el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"))
        except Exception:
            return False

    def click(self) -> None:
        if self.locator is not None:
            self.locator.click(timeout=10000)
        else:
            self.handle.click(timeout=10000)

    def clear(self) -> None:
        try:
            if self.locator is not None:
                self.locator.fill("", timeout=10000)
            else:
                self.handle.fill("", timeout=10000)
        except Exception:
            # 部分非 input 元素不支持 fill，回退键盘清空。
            self.click()
            self.page.keyboard.press("Meta+A")
            self.page.keyboard.press("Backspace")

    def _focus_without_moving_caret(self) -> None:
        """聚焦输入框但保持光标位置，兼容 Selenium send_keys 语义。

        不能用 locator.click()：click 默认点元素中心，会把光标拽到文本中间。
        逐段 send_keys（_human_type_text 一次 1~2 字符）时，邮箱输到超过半个
        输入框宽度后，每次输入前光标都会被点到中间，后续字符全部插到中间，
        出现「输 . 时光标回退、字符从光标右侧冒出来」的现象。
        elementHandle.focus()/locator.focus() 只调用 DOM focus()：已聚焦时无副作用；
        未聚焦时 Chrome 把光标放到文本末尾（与 Selenium send_keys 一致）。
        """
        try:
            if self.handle is not None:
                self.handle.focus()
                return
        except Exception:
            pass
        try:
            if self.locator is not None:
                self.locator.focus(timeout=5000)
        except Exception:
            pass

    @property
    def tag_name(self) -> str:
        try:
            return str(self._eval("el => el.tagName.toLowerCase()") or "")
        except Exception:
            return ""

    def send_keys(self, *values: str) -> None:
        # 兼容 Selenium: el.send_keys(Keys.COMMAND, 'a')、Keys.BACKSPACE 等。
        # 注意：普通文本必须是「追加」语义（对应 Selenium send_keys），不能用
        # Playwright fill()——fill() 会整体替换，逐段 send_keys 时每段都会把
        # 之前的内容覆盖掉，输入框永远只剩最后一段字符。
        # 也绝不能先 click()：click 点元素中心会移动光标，逐段输入时邮箱输到
        # 一定长度后光标被拽回中间，后续字符全部插到中间（见 _focus_without_moving_caret）。
        text = "".join(str(v or "") for v in values)
        lower = text.lower()
        # 普通文本由 press_sequentially 内部聚焦（不移动光标）；按键类分支需要
        # 先聚焦，这里统一做一次不移动光标的聚焦。绝不能 click()（会把光标点到元素中心）。
        self._focus_without_moving_caret()
        # Selenium Keys 私有区编码 → Playwright 按键
        if "\ue003" in text:      # BACKSPACE
            self.page.keyboard.press("Backspace")
            return
        if "\ue017" in text:      # DELETE
            self.page.keyboard.press("Delete")
            return
        if "\ue007" in text:      # ENTER
            self.page.keyboard.press("Enter")
            return
        if "\ue00c" in text:      # ESCAPE
            self.page.keyboard.press("Escape")
            return
        if "\ue009" in text or "\ue03d" in text or "command" in lower or "control" in lower:
            # Selenium Keys.CONTROL/COMMAND 编码可能传入私有区字符；这里按全选处理。
            try:
                self.page.keyboard.press("Control+A")
            except Exception:
                self.page.keyboard.press("Meta+A")
            return
        try:
            if self.locator is not None:
                self.locator.press_sequentially(text, timeout=10000)
            else:
                self.handle.type(text, timeout=10000)
        except Exception:
            self.page.keyboard.type(text, delay=35)

    def get_attribute(self, name: str) -> str | None:
        try:
            if self.locator is not None:
                return self.locator.get_attribute(name, timeout=1000)
            return self.handle.get_attribute(name)
        except Exception:
            return None


_NODE_REF_MARK = "ref: <Node>"


def _contains_node_ref(value: Any, depth: int = 0) -> bool:
    """递归判断 Python 结构里是否含 Playwright 对元素句柄的序列化占位。

    page.evaluate_handle 返回的对象里若嵌了 DOM 元素，json_value() 会把它们
    序列化成 'ref: <Node>' 字符串（Roxy/Selenium 则会返回可用的 WebElement proxy）。
    据此判断是否需要按属性递归重建元素句柄。
    """
    if depth > 8:
        return False
    if isinstance(value, str):
        return _NODE_REF_MARK in value
    if isinstance(value, dict):
        return any(_contains_node_ref(v, depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_node_ref(v, depth + 1) for v in value)
    return False


def _unwrap_prop(page: Any, handle: Any, key: str, depth: int) -> Any:
    """取一个属性并递归拆解；只在拆解失败时释放句柄。

    成功路径的释放由 _unwrap_js_result 自己负责：普通值路径会 dispose，
    元素路径则把句柄交给 CloakElement 持有（不能在此重复 dispose，否则
    调用方拿到的是已失效句柄）。
    """
    prop = None
    try:
        prop = handle.get_property(key)
        return CloakSeleniumDriver._unwrap_js_result(page, prop, depth + 1)
    except Exception:
        if prop is not None:
            try:
                prop.dispose()
            except Exception:
                pass
        return None


def _unwrap_object_handle(page: Any, handle: Any, depth: int = 0) -> Any:
    """把 JSHandle 指向的对象/数组按属性递归转成 Python 值，元素转成 CloakElement。

    仅在返回值里出现元素句柄时调用：逐个属性取回真实句柄，元素属性替换成
    CloakElement，普通值递归展开，使 Selenium 风格的调用方（_human_click、
    _human_type_text 等）拿到可用的元素对象。
    """
    if depth > 8:
        return None
    try:
        is_array = bool(handle.evaluate("o => Array.isArray(o)"))
        keys = handle.evaluate("o => Object.keys(o)")
    except Exception:
        return None
    if not isinstance(keys, list):
        return None
    if is_array:
        return [_unwrap_prop(page, handle, k, depth) for k in keys]
    return {k: _unwrap_prop(page, handle, k, depth) for k in keys}


class _SwitchTo:
    def __init__(self, driver: "CloakSeleniumDriver"):
        self._driver = driver

    def window(self, handle: str) -> None:
        self._driver._switch_window(handle)


class CloakSeleniumDriver:
    """只实现本项目 Roxy Selenium 流程实际用到的 WebDriver 子集。"""

    def __init__(self, browser: Any, context: Any | None, page: Any):
        self.browser = browser
        self.context = context
        self.page = page
        self._page_load_timeout_ms = int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90) * 1000
        self._script_timeout_ms = self._page_load_timeout_ms
        self.switch_to = _SwitchTo(self)
        # 前置代理转发器停止回调；build_cloak_driver 启动转发器后设置，quit() 时释放。
        self._forwarder_stop = None

    @property
    def current_url(self) -> str:
        return str(getattr(self.page, "url", "") or "")

    @property
    def window_handles(self) -> list[str]:
        pages = self._pages()
        return [str(i) for i in range(len(pages))]

    def _pages(self) -> list[Any]:
        try:
            if self.context is not None:
                return list(self.context.pages)
        except Exception:
            pass
        try:
            contexts = list(getattr(self.browser, "contexts", []) or [])
            pages = []
            for ctx in contexts:
                pages.extend(list(getattr(ctx, "pages", []) or []))
            return pages or [self.page]
        except Exception:
            return [self.page]

    def _switch_window(self, handle: str) -> None:
        pages = self._pages()
        idx = int(handle)
        self.page = pages[idx]
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def set_page_load_timeout(self, seconds: int) -> None:
        self._page_load_timeout_ms = int(seconds) * 1000
        try:
            self.page.set_default_navigation_timeout(self._page_load_timeout_ms)
            self.page.set_default_timeout(self._page_load_timeout_ms)
        except Exception:
            pass

    def get_script_timeout(self) -> int:
        """读取脚本执行超时（秒）；兼容 _enable_2fa_in_roxy 的临时放宽/恢复。"""
        return int(getattr(self, "_script_timeout_ms", self._page_load_timeout_ms) or 0) // 1000

    def set_script_timeout(self, seconds: int) -> None:
        # Playwright 的 page.evaluate 执行时长由本适配层的包装脚本内部计时
        # （默认 120s），页面 default_timeout 只影响点击/填表等动作等待，因此
        # 这里只记录数值供 get_script_timeout 回读，不覆盖页面 default_timeout。
        self._script_timeout_ms = max(0, int(seconds or 0)) * 1000

    def get(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def refresh(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=self._page_load_timeout_ms)

    def quit(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        # 释放本会话启动的前置代理转发器（端口/线程）
        if self._forwarder_stop is not None:
            try:
                self._forwarder_stop()
            except Exception as exc:
                logger.debug("[Cloak] 关闭前置代理转发器失败：%s", exc)
            finally:
                self._forwarder_stop = None

    def find_elements(self, by: Any, selector: str) -> list[CloakElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> CloakElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    def _locator(self, by: Any, selector: str):
        by_s = str(by or "").lower()
        if "xpath" in by_s or str(selector).startswith("//"):
            return self.page.locator(f"xpath={selector}")
        return self.page.locator(selector)

    def execute_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=False)

    def execute_async_script(self, script: str, *args: Any) -> Any:
        return self._evaluate(script, args=args, async_mode=True)

    def execute_cdp_cmd(self, cmd: str, params: dict | None = None) -> Any:
        params = params or {}
        try:
            client = self.context.new_cdp_session(self.page) if self.context is not None else self.page.context.new_cdp_session(self.page)
            return client.send(cmd, params)
        except Exception as exc:
            logger.debug("[Cloak] CDP 命令失败 %s: %s", cmd, exc)
            return None

    def _serialize_args(self, args: tuple[Any, ...]) -> tuple[CloakElement | None, list[Any]]:
        """拆分 Selenium 脚本参数。

        Playwright 的 JSHandle/ElementHandle 不能可靠地嵌在 dict/list payload 中跨
        page.evaluate 传递；Selenium 脚本最常见模式是 `arguments[0]` 为元素，
        因此这里把第一个 CloakElement 作为真实 DOM `el` 传入，其它参数保持
        JSON 可序列化。
        """
        first_el = args[0] if args and isinstance(args[0], CloakElement) else None
        rest = list(args[1:] if first_el else args)
        cleaned = []
        for item in rest:
            if isinstance(item, CloakElement):
                # 极少数脚本会传多个元素；用真实 handle 直接会在嵌套 payload 中失效，
                # 这里退化为 None，比把错误对象传进 JS 更安全。
                cleaned.append(None)
            else:
                cleaned.append(item)
        return first_el, cleaned

    @staticmethod
    def _unwrap_js_result(page, handle: Any, depth: int = 0) -> Any:
        # 顶层就是元素 → CloakElement 持有句柄，后续会被 Selenium 风格代码
        # （_human_click/_human_type_text 等）使用，不能 dispose。
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return CloakElement(page, handle=element)
        try:
            try:
                raw = handle.json_value()
            except Exception as exc:
                msg = str(exc)
                if "Execution context was destroyed" in msg or "navigation" in msg.lower():
                    logger.info("[Cloak] JS 执行后页面发生跳转，忽略返回值读取失败：%s", msg[:160])
                    return {"ok": True, "reason": "navigation_after_script"}
                # json_value 失败可能是旧版 Playwright 无法序列化含元素句柄的对象；
                # 尝试按属性递归重建，元素替换成 CloakElement。
                if depth < 6:
                    rebuilt = _unwrap_object_handle(page, handle, depth + 1)
                    if rebuilt is not None:
                        return rebuilt
                raise
            if depth >= 6 or not _contains_node_ref(raw):
                return raw
            rebuilt = _unwrap_object_handle(page, handle, depth + 1)
            return rebuilt if rebuilt is not None else raw
        finally:
            try:
                handle.dispose()
            except Exception:
                pass

    def _evaluate(self, script: str, args: tuple[Any, ...], async_mode: bool) -> Any:
        first_el, serial_args = self._serialize_args(args)
        if async_mode:
            wrapper = """async ({script, args}) => {
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            element_wrapper = """async (el, payload) => {
              const args = [el, ...payload.args];
              return await new Promise((resolve) => {
                const fn = new Function(...args.map((_, i) => 'a' + i), '__cloak_done', payload.script);
                const timer = setTimeout(() => resolve({__cloak_timeout:true}), 120000);
                const __cloak_done = (v) => { clearTimeout(timer); resolve(v); };
                try { fn(...args, __cloak_done); } catch (e) { clearTimeout(timer); resolve({ok:false, error:String(e)}); }
              });
            }"""
            if first_el is not None:
                result = first_el._eval(element_wrapper, {"script": script, "args": serial_args})
            else:
                result = self.page.evaluate(wrapper, {"script": script, "args": serial_args})
            if isinstance(result, dict) and result.get("__cloak_timeout"):
                raise TimeoutError("execute_async_script timeout")
            return result

        # Selenium 脚本经常以 `return ...` 为主体；用 Function 保持语义。
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        element_wrapper = """(el, payload) => {
          const args = [el, ...payload.args];
          const fn = new Function(...args.map((_, i) => 'a' + i), payload.script);
          return fn(...args);
        }"""
        if first_el is not None:
            handle = first_el._eval_handle(element_wrapper, {"script": script, "args": serial_args})
        else:
            handle = self.page.evaluate_handle(wrapper, {"script": script, "args": serial_args})
        return self._unwrap_js_result(self.page, handle)


def _normalize_proxy(proxy: str | None) -> str | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    return proxy.replace("socks5h://", "socks5://")


def _unique_proxy_candidates(proxy: str | None, use_proxy: bool) -> tuple[list[str], bool]:
    """Return the candidates for one window and whether selection came from the pool."""
    if not use_proxy:
        return [], False
    if proxy is not None:
        value = _normalize_proxy(proxy)
        return ([value] if value else []), False

    try:
        from config import proxy as _proxy_cfg
        pool = list(getattr(_proxy_cfg, "PROXY_POOL", []) or [])
    except Exception:
        pool = []
    candidates = []
    seen = set()
    for value in pool:
        normalized = _normalize_proxy(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)
    random.SystemRandom().shuffle(candidates)
    return candidates[:_CLOAK_PROXY_MAX_ATTEMPTS], True


def _close_forwarder(stop) -> None:
    if stop is None:
        return
    try:
        stop()
    except Exception:
        logger.debug("[Cloak] 清理代理转发器失败", exc_info=True)


def _start_cloak_proxy_chain(proxy_url: str):
    """Build the effective proxy URL used by both health check and Cloak."""
    try:
        from config.proxy import PROXY_PRE_PROXY as _pre_proxy
    except Exception:
        _pre_proxy = ""
    pre_proxy = str(_pre_proxy or "").strip()
    if not pre_proxy:
        return proxy_url, None, ""
    from core.upstream_proxy import start_forwarder
    fwd = start_forwarder(upstream=proxy_url, pre_proxy=pre_proxy)
    return fwd["url"], fwd["stop"], pre_proxy


def _mask_proxy_url(proxy: str | None) -> str:
    """返回可用于日志的代理摘要，不泄露用户名/密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(value if "://" in value else f"//{value}")
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if (parsed.username or parsed.password) else ""
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _detect_cloak_exit_geo(proxy_url: str | None = None) -> dict:
    """按当前/代理出口检测地理信息，供 Cloak 显式 locale/timezone 使用。"""
    try:
        import requests
        from config import browser as _browser_cfg
        endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        timeout = float(getattr(_browser_cfg, "IP_GEO_TIMEOUT", 6) or 6)
    except Exception:
        return {}
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, proxies=proxies, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            timezone = data.get("timezone")
            if isinstance(timezone, dict):
                timezone = timezone.get("id") or timezone.get("name")
            geo = {
                "ip": data.get("ip") or data.get("query"),
                "country": (data.get("country") or data.get("country_code") or data.get("countryCode") or "").upper(),
                "region": data.get("region") or data.get("regionName"),
                "city": data.get("city"),
                "timezone": timezone or "",
                "org": data.get("org") or data.get("isp") or (data.get("connection") or {}).get("org"),
            }
            if geo.get("country") or geo.get("timezone"):
                logger.info(
                    "[Cloak] 出口IP地理信息：ip=%s country=%s city=%s timezone=%s",
                    geo.get("ip") or "?", geo.get("country") or "?", geo.get("city") or "?", geo.get("timezone") or "?",
                )
                return geo
        except Exception as exc:
            logger.debug("[Cloak] 出口 IP 地理检测失败 endpoint=%s: %s: %s", url, type(exc).__name__, exc)
    return {}


def _build_cloak_locale_options(proxy_url: str | None = None) -> dict:
    """生成传给 Cloak 原生启动参数的显式语言/时区配置。

    GeoIP 解析由 Cloak 的 ``launch``/``launch_context`` 单独负责。这里再用
    requests 解析一次会让动态住宅代理在两次连接间轮换出口，造成 locale、
    timezone 和 WebRTC IP 互相不一致。
    """
    explicit_locale = str(getattr(_cfg, "CLOAK_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CLOAK_TIMEZONE", "") or "").strip()
    out = {}
    if explicit_locale:
        out["locale"] = explicit_locale
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    return {k: v for k, v in out.items() if v}


def _check_cloak_registration_route(
    driver: CloakSeleniumDriver,
    opened: CloakOpenResult,
    registration_country: str = "",
) -> tuple[dict[str, str], str]:
    """Measure the route in the launched browser before registration starts.

    The browser measurement is authoritative for the IP.  A requests-based
    proxy health check may use a different connection (and rotating residential
    proxies can assign it a different IP), so its IP must never hide a browser
    probe timeout.  The health result is only used to fill a missing browser
    country, which is useful for IP-only browser probe endpoints.
    """
    from core.registration_ip import (
        detect_selenium_registration_geo,
        normalize_registration_geo,
        registration_geo_from_open_result,
        registration_geo_matches,
    )

    browser_geo = normalize_registration_geo(
        detect_selenium_registration_geo(
            driver,
            log_prefix="[Cloak注册]",
            require_country=bool(registration_country),
        )
    )
    launch_geo = registration_geo_from_open_result(opened.raw or {})
    registration_geo = {
        # Deliberately do not fall back to proxy_health.observed_ip here.  Every
        # registration candidate must prove that the browser itself has a route.
        "ip": browser_geo["ip"],
        "country": browser_geo["country"] or launch_geo["country"],
    }
    route_ok, route_error = registration_geo_matches(
        registration_geo,
        registration_country,
    )
    if route_ok:
        raw = opened.raw if isinstance(opened.raw, dict) else {}
        opened.raw = raw
        raw["registration_ip"] = registration_geo["ip"]
        raw["registration_country"] = registration_geo["country"]
        raw["registration_geo"] = dict(registration_geo)
        logger.info(
            "[Cloak注册] 代理预检通过：IP=%s country=%s",
            registration_geo["ip"],
            registration_geo["country"] or "?",
        )
        return registration_geo, ""
    return registration_geo, route_error


def build_cloak_driver(
    proxy: str | None = None,
    registration_country: str = "",
    *,
    registration_preflight: bool = False,
) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """启动 CloakBrowser 并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取；
    proxy=""    时显式禁用代理；
    proxy="..." 时使用指定代理。
    """
    use_proxy = bool(getattr(_cfg, "CLOAK_USE_PROXY", True))
    from core.registration_ip import normalize_country_code
    registration_country = normalize_country_code(
        registration_country,
        strict=bool(str(registration_country or "").strip()),
    )
    candidates, from_pool = _unique_proxy_candidates(proxy, use_proxy)
    candidates = list(candidates)[:_CLOAK_PROXY_MAX_ATTEMPTS]
    try:
        from cloakbrowser import launch_context, launch_persistent_context
    except ImportError as exc:
        raise RuntimeError("未安装 cloakbrowser，请执行：pip install cloakbrowser") from exc

    launch_args = list(getattr(_cfg, "CLOAK_EXTRA_ARGS", []) or [])
    seed = str(getattr(_cfg, "CLOAK_FINGERPRINT_SEED", "") or "").strip()
    if seed:
        launch_args.append(f"--fingerprint={seed}")

    if candidates:
        try:
            from config import proxy as _proxy_cfg
        except Exception:
            _proxy_cfg = None
        health_enabled = bool(getattr(_proxy_cfg, "PROXY_HEALTH_CHECK", True))
        health_timeout = float(getattr(_proxy_cfg, "PROXY_HEALTH_TIMEOUT", 8.0) or 8.0)
        try:
            from config import browser as _browser_cfg
            health_endpoints = list(getattr(_browser_cfg, "IP_GEO_ENDPOINTS", []) or [])
        except Exception:
            health_endpoints = []
        last_error = ""
        for attempt, candidate in enumerate(candidates, start=1):
            candidate_stop = None
            proxy_health = None
            launch_attempted = False
            try:
                effective, candidate_stop, candidate_pre_proxy = _start_cloak_proxy_chain(candidate)
                if candidate_pre_proxy:
                    logger.info(
                        "[Cloak] 代理前置链已建立：upstream=%s pre_proxy=%s -> %s",
                        _mask_proxy_url(candidate), _mask_proxy_url(candidate_pre_proxy), _mask_proxy_url(effective),
                    )
                if health_enabled:
                    health = check_proxy_health(
                        effective,
                        timeout=max(1.0, health_timeout),
                        endpoints=health_endpoints,
                    )
                    if not health.ok:
                        last_error = health.error or "代理测活失败"
                        logger.warning(
                            "[Cloak] 代理测活失败，立即切换下一条：proxy=%s endpoint=%s error=%s",
                            _mask_proxy_url(candidate), health.endpoint or "-", last_error[:180],
                        )
                        _close_forwarder(candidate_stop)
                        continue
                    observed_country = normalize_country_code(health.country)
                    proxy_health = {
                        "ok": True,
                        "observed_ip": health.observed_ip,
                        "endpoint": health.endpoint,
                        "latency_ms": health.latency_ms,
                        "country": observed_country,
                    }
                    logger.info(
                        "[Cloak] 代理测活通过：proxy=%s observed_ip=%s country=%s latency=%sms",
                        _mask_proxy_url(candidate), health.observed_ip or "?",
                        observed_country or "?", health.latency_ms,
                    )
                locale_opts = _build_cloak_locale_options(effective)
                launch_attempted = True
                driver, opened = _launch_cloak_context(
                    effective_proxy_url=effective,
                    upstream_proxy_url=candidate,
                    forwarder_stop=candidate_stop,
                    proxy_health=proxy_health,
                    locale_opts=locale_opts,
                    launch_args=launch_args,
                    launch_context=launch_context,
                    launch_persistent_context=launch_persistent_context,
                )
                if registration_preflight:
                    try:
                        _registration_geo, route_error = _check_cloak_registration_route(
                            driver,
                            opened,
                            registration_country,
                        )
                    except Exception as exc:
                        route_error = f"浏览器出口 Geo 检测失败：{type(exc).__name__}: {exc}"
                    if route_error:
                        last_error = route_error
                        logger.warning(
                            "[Cloak注册] 代理预检失败，关闭当前窗口并切换下一条："
                            "proxy=%s attempt=%s/%s error=%s",
                            _mask_proxy_url(candidate),
                            attempt,
                            len(candidates),
                            route_error[:180],
                        )
                        driver.quit()
                        continue
                return driver, opened
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                retryable = _is_retryable_cloak_launch_error(exc)
                logger.warning(
                    "[Cloak] 代理准备/浏览器启动失败：proxy=%s attempt=%s/%s retryable=%s error=%s",
                    _mask_proxy_url(candidate), attempt, len(candidates), retryable, last_error[:180],
                )
                # _launch_cloak_context owns cleanup after launch begins;
                # preparation/health-check failures are cleaned here.  The
                # stop callback is handled by that helper when context launch
                # was attempted.  Only preparation/health-check failures need
                # cleanup here, avoiding a duplicate callback invocation.
                if not launch_attempted:
                    _close_forwarder(candidate_stop)
                if not retryable:
                    raise
        else:
            source = "代理池" if from_pool else "指定代理"
            raise RuntimeError(f"{source}中没有可用代理，未启动浏览器窗口：{last_error[:180] or '测活失败'}")

    driver, opened = _launch_cloak_context(
        effective_proxy_url=None,
        upstream_proxy_url=None,
        forwarder_stop=None,
        proxy_health=None,
        locale_opts=_build_cloak_locale_options(None),
        launch_args=launch_args,
        launch_context=launch_context,
        launch_persistent_context=launch_persistent_context,
    )
    if registration_preflight:
        try:
            _registration_geo, route_error = _check_cloak_registration_route(
                driver,
                opened,
                registration_country,
            )
        except Exception as exc:
            route_error = f"浏览器出口 Geo 检测失败：{type(exc).__name__}: {exc}"
        if route_error:
            driver.quit()
            raise RuntimeError(route_error)
    return driver, opened


def _launch_cloak_context(
    *,
    effective_proxy_url: str | None,
    upstream_proxy_url: str | None,
    forwarder_stop,
    proxy_health: dict | None,
    locale_opts: dict,
    launch_args: list,
    launch_context,
    launch_persistent_context,
) -> tuple[CloakSeleniumDriver, CloakOpenResult]:
    """Launch one Cloak context for a fully prepared proxy candidate."""
    # geoip=True 交给 CloakBrowser 根据当前出口 IP 自动匹配 timezone/locale/WebRTC。
    # 之前只有显式 proxy_url 时才开启；如果用户走系统代理/VPN/透明代理，代码层面
    # 看不到 proxy_url，会误关 geoip，导致语言/时区不跟随出口。这里改为完全尊重配置。
    opts = {
        "headless": bool(getattr(_cfg, "CLOAK_HEADLESS", False)),
        "humanize": bool(getattr(_cfg, "CLOAK_HUMANIZE", True)),
        "geoip": bool(getattr(_cfg, "CLOAK_GEOIP", True)),
    }
    if locale_opts.get("locale"):
        opts["locale"] = locale_opts["locale"]
    if locale_opts.get("timezone"):
        opts["timezone"] = locale_opts["timezone"]
    if effective_proxy_url:
        opts["proxy"] = effective_proxy_url
    if launch_args:
        opts["args"] = launch_args
    license_key = str(getattr(_cfg, "CLOAK_LICENSE_KEY", "") or "").strip()
    if license_key:
        opts["license_key"] = license_key
    runtime_status = _cloak_runtime_status(license_key)
    if runtime_status and not runtime_status.get("keyed"):
        logger.warning(
            "[Cloak] 当前为无密钥 binary=%s（wrapper=%s）；运行 cloakbrowser login 可切换到当前免费 binary",
            runtime_status.get("browser_version") or "?",
            runtime_status.get("wrapper_version") or "?",
        )

    user_data_dir = str(getattr(_cfg, "CLOAK_USER_DATA_DIR", "") or "").strip()
    logger.info(
        "[Cloak] 启动 CloakBrowser：headless=%s humanize=%s geoip=%s proxy=%s locale=%s timezone=%s persistent=%s",
        opts.get("headless"), opts.get("humanize"), opts.get("geoip"),
        effective_proxy_url or "无", opts.get("locale") or "自动/默认", opts.get("timezone") or "自动/默认",
        bool(user_data_dir),
    )
    # locale/timezone 只经 Cloak 的 Chromium 原生 flag 设置。再传给 Playwright
    # context 会启用可观察的 CDP emulation，并与原生画像形成双层覆盖。
    context = None
    try:
        if user_data_dir:
            context = launch_persistent_context(user_data_dir, **opts)
            pages = list(getattr(context, "pages", []) or [])
            page = pages[0] if pages else context.new_page()
            browser = getattr(context, "browser", None) or context
        else:
            context = launch_context(**opts)
            browser = getattr(context, "browser", None) or context
            page = context.new_page()

        driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
        driver._forwarder_stop = forwarder_stop
        # Roxy/Cloak 共用部分页面操作函数；给共享函数一个显式日志前缀，
        # 避免 Cloak 注册流程里出现 `[Roxy注册]`。
        driver._registration_log_prefix = "[Cloak注册]"
        driver.set_page_load_timeout(int(getattr(_cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
        return driver, CloakOpenResult(raw={"driver": "cloakbrowser", "fingerprint_mode": "cloak-native", "proxy": effective_proxy_url, "upstream_proxy": upstream_proxy_url, "proxy_health": proxy_health, "locale": locale_opts, "runtime": runtime_status, "options": {k: v for k, v in opts.items() if k != "license_key"}})
    except BaseException:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        _close_forwarder(forwarder_stop)
        raise
