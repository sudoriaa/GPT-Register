# -*- coding: utf-8 -*-
"""Cloudflare / Turnstile 挑战自动处理。

背景
----
Cloak/Roxy 注册流程在提交邮箱、跳转或等待 session 时，OpenAI 的页面可能触发
Cloudflare Turnstile 人机验证：
  * 非交互式 Turnstile：widget 以「隐形」方式挂载，token 在页面加载/表单提交时
    自动生成，通常无需点击；
  * 交互式 Turnstile：challenges.cloudflare.com 的跨域 iframe 内渲染一个
    checkbox，需要点一下才触发验证；
  * 托管挑战（managed challenge）：升级为图片/音频验证，无法自动完成，只能等
    用户手动操作或放弃。

技术要点
--------
* Turnstile checkbox 位于跨域 iframe + 闭 shadow DOM 里，普通 DOM 选择器进不去；
  稳定做法是对 iframe 按坐标点击（标准 300x65 widget 的 checkbox 大约在
  iframe 左上角 (28, 30) 处）。
* 验证通过后 token 会写入父页面隐藏字段 input[name="cf-turnstile-response"]；
  通过该字段非空即可判断已解决。整页拦截（challenges.cloudflare.com）则靠
  URL 是否离开挑战页判断。
* 只有「可见的 challenges iframe」才视为交互式挑战；隐藏 iframe（隐形模式）
  不阻塞流程，交给表单提交触发。

本模块兼容两种驱动：
  - Cloak：Playwright 适配层 CloakSeleniumDriver（有 .page/.context/.browser）；
  - Roxy：真实 Selenium WebDriver。
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# 标准 Turnstile widget 里 checkbox 相对 iframe 左上角的偏移（CSS 像素）。
_TURNSTILE_CHECKBOX_X = 28
_TURNSTILE_CHECKBOX_Y = 30
_TURNSTILE_MIN_WIDTH = 100
_TURNSTILE_MIN_HEIGHT = 40

# 命中即视为 Cloudflare 挑战 iframe 的 URL 片段。
_CF_IFRAME_URL_HINTS = (
    "challenges.cloudflare.com",
    "challenge-platform",
    "cf-chl",
    "turnstile",
)

# 整页挑战的 URL 片段。
_CF_INTERSTITIAL_URL_HINTS = (
    "challenges.cloudflare.com",
    "cdn-cgi/challenge-platform",
    "cf_chl",
    "challenge-platform",
)

# 整页挑战页面正文里的特征文案。
_CF_INTERSTITIAL_TEXT_RE = (
    "verify you are human",
    "请稍候",
    "验证您是人类",
    "one more step",
    "checking your browser",
)


def _is_cloak_driver(driver: Any) -> bool:
    """Playwright 适配层 driver 有 .page 属性，Selenium WebDriver 没有。"""
    return bool(getattr(driver, "page", None))


def _clickable_turnstile_box(box: Any, viewport: Any = None) -> bool:
    """过滤隐形/离屏 iframe，只保留能容纳 checkbox 点击点的可见区域。"""
    if not isinstance(box, dict):
        return False
    try:
        x = float(box.get("x", 0) or 0)
        y = float(box.get("y", 0) or 0)
        width = float(box.get("w", box.get("width", 0)) or 0)
        height = float(box.get("h", box.get("height", 0)) or 0)
    except (TypeError, ValueError):
        return False
    if width < _TURNSTILE_MIN_WIDTH or height < _TURNSTILE_MIN_HEIGHT:
        return False
    click_x = x + _TURNSTILE_CHECKBOX_X
    click_y = y + _TURNSTILE_CHECKBOX_Y
    if click_x < 0 or click_y < 0:
        return False
    if isinstance(viewport, dict):
        try:
            viewport_width = float(viewport.get("width", 0) or 0)
            viewport_height = float(viewport.get("height", 0) or 0)
            if viewport_width > 0 and click_x >= viewport_width:
                return False
            if viewport_height > 0 and click_y >= viewport_height:
                return False
        except (TypeError, ValueError):
            pass
    return True


def _visible_cloak_challenge_frame(driver: Any) -> dict | None:
    """从 Playwright frame 树里找可见的 CF frame。

    Turnstile 经常把 iframe 放进 closed shadow root；此时主文档的
    ``querySelectorAll('iframe')`` 看不到它，但 Playwright 仍会把它暴露在
    ``page.frames``，并可通过 ``frame_element().bounding_box()`` 读取位置。
    """
    page = getattr(driver, "page", None)
    if page is None:
        return None
    try:
        frames = list(getattr(page, "frames", []) or [])
    except Exception:
        return None
    for frame in frames:
        owner = None
        try:
            src = str(getattr(frame, "url", "") or "")
            if not src or not any(h in src.lower() for h in _CF_IFRAME_URL_HINTS):
                continue
            owner = frame.frame_element()
            box = owner.bounding_box()
            if not _clickable_turnstile_box(box, getattr(page, "viewport_size", None)):
                continue
            return {
                "x": box.get("x", 0),
                "y": box.get("y", 0),
                "w": box.get("width", 0),
                "h": box.get("height", 0),
                "src": src[:180],
            }
        except Exception:
            continue
        finally:
            if owner is not None:
                try:
                    owner.dispose()
                except Exception:
                    pass
    return None


def _main_state(driver: Any) -> dict:
    """在主 frame 里读取挑战相关状态。

    返回 {url, widget, token, iframe, interstitialText, challengeForm, error?}。
    iframe 仅在「可见」时记录，隐藏 iframe 代表隐形模式，不算交互式挑战。
    """
    script = r"""
    const out = {url: location.href, widget: false, token: false, iframe: null, interstitialText: false, challengeForm: false};
    try {
      out.widget = !!document.querySelector('.cf-turnstile, [class*="cf-turnstile"], [data-sitekey]');
      const tok = document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
      out.token = !!(tok && String(tok.value || '').trim().length > 10);
      for (const f of document.querySelectorAll('iframe')) {
        const src = String(f.src || '');
        if (!/challenges\.cloudflare\.com|challenge-platform|cf-chl|turnstile/i.test(src)) continue;
        const r = f.getBoundingClientRect();
        const st = getComputedStyle(f);
        const clickX = r.x + 28;
        const clickY = r.y + 30;
        const visible = r.width >= 100 && r.height >= 40 &&
          clickX >= 0 && clickY >= 0 && clickX < innerWidth && clickY < innerHeight &&
          st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity || 1) > 0;
        if (!visible) continue;
        out.iframe = {x: r.x, y: r.y, w: r.width, h: r.height, src: src.slice(0, 180)};
        break;
      }
      const t = (document.body ? (document.body.innerText || '') : '').toLowerCase();
      out.interstitialText = /verify you are human|请稍候|验证您是人类|one more step|checking your browser/i.test(t);
      out.challengeForm = !!document.querySelector('#challenge-form, #challenge-stage, .cf-chl-widget, [id*="cf-challenge"], #trk_captcha, [class*="challenge-form"]');
    } catch (e) {
      out.error = String(e);
    }
    return out;
    """
    try:
        result = driver.execute_script(script) or {}
        if not isinstance(result, dict):
            result = {"error": "probe returned a non-object result"}
    except Exception as exc:
        result = {"url": str(getattr(driver, "current_url", "") or ""), "error": f"{type(exc).__name__}: {exc}"}
    if result.get("iframe") and not _clickable_turnstile_box(result.get("iframe")):
        result["iframe"] = None
    # A Turnstile iframe inside a closed shadow root is absent from the
    # main-document selector result. Playwright's frame tree still sees it.
    if _is_cloak_driver(driver) and not result.get("iframe"):
        frame_state = _visible_cloak_challenge_frame(driver)
        if frame_state:
            result["iframe"] = frame_state
    return result


def challenge_kind(driver: Any) -> str:
    """判断当前页面挑战类型。

    返回：
      'none'         —— 无挑战（或隐形模式，无需处理）；
      'turnstile'    —— 交互式 Turnstile checkbox 已出现（iframe 可见）；
      'interstitial' —— 整页 Cloudflare 挑战；
      'unknown'      —— 页面状态探测失败，不能据此判定为无挑战。
    """
    state = _main_state(driver)
    urls = (
        str(getattr(driver, "current_url", "") or "").lower(),
        str(state.get("url") or "").lower(),
    )
    if any(any(h in url for h in _CF_INTERSTITIAL_URL_HINTS) for url in urls):
        return "interstitial"
    if _clickable_turnstile_box(state.get("iframe")):
        return "turnstile"
    if state.get("interstitialText") or state.get("challengeForm"):
        return "interstitial"
    if state.get("error"):
        return "unknown"
    return "none"


def turnstile_solved(driver: Any) -> bool:
    """cf-turnstile-response 隐藏字段已写入 token 即视为已解决。"""
    state = _main_state(driver)
    return bool(state.get("token"))


def _click_widget_iframe(driver: Any, *, humanize: bool = True) -> bool:
    """对 Turnstile checkbox iframe 按坐标点击。

    跨域 iframe + 闭 shadow DOM 无法用 DOM 选择器进入，只能对 iframe 的
    可见区域做坐标点击。返回是否真的完成了点击动作。
    """
    if _is_cloak_driver(driver):
        return _click_widget_iframe_cloak(driver, humanize=humanize)
    return _click_widget_iframe_selenium(driver, humanize=humanize)


def _click_widget_iframe_cloak(driver: Any, *, humanize: bool = True) -> bool:
    try:
        page = driver.page
        frame_state = _visible_cloak_challenge_frame(driver)
        if frame_state:
            box = {
                "x": frame_state["x"],
                "y": frame_state["y"],
                "width": frame_state["w"],
                "height": frame_state["h"],
            }
        else:
            # Keep a selector fallback for normal (non-shadow) widgets and
            # older Playwright adapters that do not expose frame_element().
            sel = ", ".join(f"iframe[src*='{h}']" for h in _CF_IFRAME_URL_HINTS)
            loc = page.locator(sel).first
            box = loc.bounding_box()
        if not _clickable_turnstile_box(box, getattr(page, "viewport_size", None)):
            return False
        x = box["x"] + _TURNSTILE_CHECKBOX_X
        y = box["y"] + _TURNSTILE_CHECKBOX_Y
        if humanize:
            # 模拟鼠标路径：先移动到 checkbox 附近，再靠近，最后按下。
            page.mouse.move(x + 14, y + 12, steps=4)
            time.sleep(0.08)
            page.mouse.move(x, y, steps=5)
            time.sleep(0.05)
        else:
            page.mouse.move(x, y, steps=1)
        page.mouse.down()
        time.sleep(0.09)
        page.mouse.up()
        return True
    except Exception as exc:
        logger.debug("[CF] Cloak iframe 点击失败：%s: %s", type(exc).__name__, exc)
        return False


def _click_widget_iframe_selenium(driver: Any, *, humanize: bool = True) -> bool:
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By

        sel = ", ".join(f"iframe[src*='{h}']" for h in _CF_IFRAME_URL_HINTS)
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        el = None
        for e in els:
            try:
                if e.is_displayed() and _clickable_turnstile_box(getattr(e, "rect", None)):
                    el = e
                    break
            except Exception:
                continue
        if el is None:
            return False
        chains = ActionChains(driver)
        if humanize:
            chains.pause(0.15)
        chains.move_to_element_with_offset(el, _TURNSTILE_CHECKBOX_X, _TURNSTILE_CHECKBOX_Y).pause(0.12).click().perform()
        return True
    except Exception as exc:
        logger.debug("[CF] Selenium iframe 点击失败：%s: %s", type(exc).__name__, exc)
        return False


def _default_timeout() -> int:
    try:
        from config import browser as _browser_cfg
        return max(5, int(getattr(_browser_cfg, "CLOUD_FLARE_SOLVE_TIMEOUT", 60) or 60))
    except Exception:
        return 60


def _default_humanize() -> bool:
    try:
        from config import humanize as _hcfg
        return bool(getattr(_hcfg, "ENABLE_HUMANIZE_BROWSER_ACTIONS", True))
    except Exception:
        return True


def solve_cloudflare_challenge(
    driver: Any,
    *,
    timeout: int | None = None,
    label: str = "",
    log_prefix: str = "",
    humanize: bool | None = None,
) -> bool:
    """检测并自动处理当前页面上的 Cloudflare/Turnstile 挑战。

    返回 True  表示当前没有阻碍（无挑战，或挑战已自动解决）；
    返回 False 表示挑战仍存在且未能在 timeout 内自动解决（通常已升级成
    人工验证，需要人工干预）。

    timeout 内最多点击 checkbox 3 次，其余时间等待 token / URL 离开挑战页。
    无挑战时立即返回，几乎不耗时。
    """
    if humanize is None:
        humanize = _default_humanize()
    timeout = _default_timeout() if timeout is None else int(timeout)
    timeout = max(5, timeout)

    prefix = str(log_prefix or "").rstrip()
    label_txt = f" {label}" if label else ""
    end = time.time() + timeout
    clicked = 0
    last_log = 0.0
    kind = "none"

    while time.time() < end:
        kind = challenge_kind(driver)
        if kind == "none":
            return True
        if kind == "unknown":
            now = time.time()
            if now - last_log >= 3.0:
                logger.info("%s[CF]%s 页面状态探测暂时失败，继续重试，remaining=%.0fs", prefix, label_txt, end - now)
                last_log = now
            time.sleep(0.9)
            continue
        if turnstile_solved(driver):
            logger.info("%s[CF]%s Cloudflare 验证已通过（cf-turnstile-response 已写入 token）", prefix, label_txt)
            return True
        now = time.time()
        if kind == "turnstile" and clicked < 3 and now - last_log >= 0.8:
            ok = _click_widget_iframe(driver, humanize=humanize)
            if ok:
                clicked += 1
                logger.info("%s[CF]%s 已点击 Turnstile checkbox（第 %s 次）", prefix, label_txt, clicked)
        if now - last_log >= 3.0:
            logger.info(
                "%s[CF]%s 等待 Cloudflare 验证通过… kind=%s remaining=%.0fs",
                prefix, label_txt, kind, end - now,
            )
            last_log = now
        time.sleep(0.9)

    logger.warning(
        "%s[CF]%s Cloudflare 验证未在 %ss 内自动通过（kind=%s，可能升级为人工验证），请留意浏览器窗口",
        prefix, label_txt, timeout, kind,
    )
    return False
